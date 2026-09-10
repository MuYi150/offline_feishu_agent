from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import fitz

from ..fixtures import FixtureDocumentSource
from ..storage import sha256_file
from .records import digest, source_document, timestamp


class SnapshotError(RuntimeError):
    pass


def block_text(value):
    if isinstance(value, dict):
        if isinstance(value.get("text_run"), dict):
            return str(value["text_run"].get("content", ""))
        if isinstance(value.get("equation"), dict):
            return str(value["equation"].get("content", ""))
        return "".join(block_text(v) for k, v in value.items() if k not in {"children", "_children"})
    if isinstance(value, list):
        return "".join(block_text(x) for x in value)
    return ""


class BlocksAdapter:
    def __init__(self, gateway):
        self.gateway = gateway
        self.embedded = []

    def read_embedded(self, kind, *identity):
        if kind == "sheet":
            rows = self.gateway.read_embedded_sheet(*identity) if hasattr(self.gateway, "read_embedded_sheet") else self.gateway.read_values(*identity, "A:Z")
        else:
            rows = self.gateway.read_bitable(*identity)
        self.embedded.append({"kind": kind, "identity": identity, "hash": digest(rows)})
        return rows

    def verify_embedded(self):
        original = list(self.embedded)
        for item in original:
            if digest(self.read_embedded(item["kind"], *item["identity"])) != item["hash"]:
                raise SnapshotError("embedded_content_changed_during_capture")
        self.embedded = original

    def adapt(self, raw, obj):
        if not raw:
            raise SnapshotError("blocks_unavailable")
        by_id = {b["block_id"]: b for b in raw if b.get("block_id")}
        referenced = {x for b in raw for x in b.get("children", []) if isinstance(x, str)}
        roots = [b for b in raw if b.get("block_id") not in referenced and
                 (not b.get("parent_id") or b.get("parent_id") == obj or b.get("block_id") == obj)]
        if not roots:
            raise SnapshotError("blocks_root_missing")
        seen, attachments = set(), []

        def children(block):
            result = []
            refs = block.get("_children", block.get("children", []))
            if not refs and block.get("block_id"):
                refs = [b for b in raw if b.get("parent_id") == block["block_id"]]
            for child in refs:
                value = child if isinstance(child, dict) else by_id.get(child)
                if value is None:
                    raise SnapshotError("block_child_missing")
                result.append(value)
            return result

        def render(block, depth=0):
            if depth > 64:
                raise SnapshotError("blocks_depth_exceeded")
            bid = block.get("block_id")
            if bid in seen:
                raise SnapshotError("blocks_duplicate_or_cycle")
            if bid:
                seen.add(bid)
            kind = str(block.get("block_type", block.get("type", "text")))
            if kind.isdigit():
                number = int(kind)
                kind = {1: "page", 2: "text", 12: "bullet", 13: "ordered", 14: "code", 15: "quote",
                        23: "file", 27: "image", 30: "sheet", 31: "table", 32: "table_cell",
                        18: "bitable", 22: "divider", 24: "grid", 25: "grid_column", 19: "callout"}.get(number, kind)
                if 3 <= number <= 11:
                    kind = f"heading{number - 2}"
            nested = children(block)
            content = block_text(block)
            if kind == "table":
                table = block.get("table", {})
                cells = nested
                if table.get("cells"):
                    cells = [by_id.get(x) for x in table["cells"]]
                    if any(x is None for x in cells):
                        raise SnapshotError("table_cell_missing")
                columns = int(table.get("property", {}).get("column_size") or table.get("column_size") or 0)
                if not cells or not columns:
                    raise SnapshotError("table_structure_missing")
                values = []
                for cell in cells:
                    parts = render(cell, depth + 1)
                    value = "<br>".join(x.get("markdown") or x.get("text") or x.get("caption", "") for x in parts).replace("|", "\\|").replace("\n", "<br>")
                    values.append(value)
                if len(values) % columns:
                    raise SnapshotError("table_cells_incomplete")
                rows = [values[n:n + columns] for n in range(0, len(values), columns)]
                md = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * columns) + " |"]
                md.extend("| " + " | ".join(row) + " |" for row in rows[1:])
                return [{"type": "table", "markdown": "\n".join(md)}]
            if kind == "image":
                return [{"type": "image", "caption": content or "原始图片见 source.pdf"}]
            if kind in {"file", "media", "attachment"}:
                meta = block.get("file") or block.get("media") or block.get("attachment") or {}
                attachments.append({"name": meta.get("name", "未命名附件"), "token": meta.get("token", ""),
                                    "size": meta.get("size"), "type": kind})
                return [{"type": "paragraph", "text": f"[附件元数据：{attachments[-1]['name']}；内容未打开]"}]
            if kind == "sheet":
                ref = (block.get("sheet") or {}).get("token", "")
                if "_" not in ref:
                    raise SnapshotError("embedded_sheet_token_invalid")
                spreadsheet, sheet_id = ref.split("_", 1)
                rows = self.read_embedded("sheet", spreadsheet, sheet_id)
                if not rows:
                    raise SnapshotError("embedded_sheet_unavailable")
                width = max(map(len, rows))
                rows = [[str(x).replace("|", "\\|").replace("\n", "<br>") for x in r] + [""] * (width - len(r)) for r in rows]
                md = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
                md += ["| " + " | ".join(r) + " |" for r in rows[1:]]
                return [{"type": "table", "markdown": "\n".join(md)}]
            if kind == "bitable":
                ref = (block.get("bitable") or {}).get("token", "")
                if "_" not in ref:
                    raise SnapshotError("embedded_bitable_token_invalid")
                rows = self.read_embedded("bitable", *ref.split("_", 2))
                if not rows:
                    raise SnapshotError("embedded_bitable_unavailable")
                from .records import text
                md = ["| " + " | ".join(text(x).replace("|", "\\|").replace("\n", "<br>") for x in row) + " |" for row in rows]
                md.insert(1, "| " + " | ".join(["---"] * len(rows[0])) + " |")
                return [{"type": "table", "markdown": "\n".join(md)}]
            result = []
            if content and kind != "page":
                if kind.startswith("heading"):
                    result.append({"type": "heading", "level": min(6, int(kind[7:])), "text": content})
                elif kind == "code":
                    result.append({"type": "code", "language": "", "text": content})
                else:
                    result.append({"type": "list" if kind in {"bullet", "ordered"} else "quote" if kind == "quote" else "paragraph", "text": content})
            elif not nested and kind not in {"page", "text", "divider", "grid", "grid_column", "table_cell"}:
                raise SnapshotError(f"unsupported_empty_block_{kind}")
            for child in nested:
                result.extend(render(child, depth + 1))
            return result

        result = []
        for root in roots:
            result.extend(render(root))
        if set(by_id) - seen:
            raise SnapshotError("unreachable_document_blocks")
        return result, attachments


class SnapshotBuilder:
    def __init__(self, gateway, settings, root):
        self.gateway, self.settings, self.root = gateway, settings, root

    def capture(self, record, cycle):
        before = self.gateway.node(record["node"])
        self.verify_node(record, before)
        identity_hash = digest([record["node"], record["obj"], record["updated"], record["round"], record["method"],
                                record["wiki_name"], record["author"], cycle])
        key = re.sub(r"[^A-Za-z0-9_-]", "", record["obj"])[:32] + "-" + identity_hash[:20]
        target = self.root / "snapshots" / key
        if target.exists():
            manifest = self.verify_files(target)
            return key, target, manifest
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".capture-", dir=target.parent))
        try:
            raw = self.gateway.blocks(record["obj"])
            (staging / "raw_blocks.json").write_text(json.dumps({"blocks": raw}, ensure_ascii=False, indent=2), encoding="utf-8")
            adapter = BlocksAdapter(self.gateway)
            blocks, attachments = adapter.adapt(raw, record["obj"])
            pdf = self.gateway.export_pdf(record["obj"], self.settings.max_pdf_bytes)
            with fitz.open(stream=pdf, filetype="pdf") as document:
                if not 0 < document.page_count <= self.settings.max_pdf_pages:
                    raise SnapshotError("pdf_page_limit")
            try:
                author = self.gateway.user(before.get("owner"))
            except Exception:
                author = {}
            source = source_document(record, key, author.get("open_id", ""))
            payloads = {"source_document.json": source.model_dump(mode="json"),
                        "document_blocks.json": {"blocks": blocks}, "raw_blocks.json": {"blocks": raw},
                        "attachment_metadata.json": {"document_id": record["obj"], "attachments": attachments},
                        "fixture_options.json": {"source_pdf": "source.pdf", "pdf_required": True, "real_model_only": True}}
            for name, value in payloads.items():
                (staging / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            (staging / "source.pdf").write_bytes(pdf)
            adapter.verify_embedded()
            self.verify_node(record, self.gateway.node(record["node"]))
            FixtureDocumentSource().load(staging)
            manifest = {"job_id": key, "node": record["node"], "obj": record["obj"], "version": record["updated"],
                        "cycle": cycle, "record": record, "source_hash": digest(blocks), "errors": [], "embedded": adapter.embedded,
                        "files": {p.name: sha256_file(p) for p in staging.iterdir()}}
            (staging / "snapshot_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(staging, target)
            return key, target, manifest
        except Exception as exc:
            # Failed captures are diagnostic only, never published or enqueued.
            from ..storage import utc_run_id
            failed = self.root / "capture_errors" / (key + "-" + utc_run_id())
            failed.parent.mkdir(parents=True, exist_ok=True)
            error = {"node": record["node"], "obj": record["obj"], "version": record["updated"],
                     "error": getattr(exc, "code", str(exc) if isinstance(exc, SnapshotError) else type(exc).__name__)}
            (staging / "capture_error.json").write_text(json.dumps(error, ensure_ascii=False), encoding="utf-8")
            os.replace(staging, failed)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)  # Exact freshly-created staging directory only.

    @staticmethod
    def verify_node(record, node):
        if node.get("obj_type") != "docx" or node.get("obj_token") != record["obj"]:
            raise SnapshotError("document_identity_changed_or_not_docx")
        if not record["updated"] or timestamp(node.get("edit_time")) != record["updated"]:
            raise SnapshotError("document_version_changed")

    @staticmethod
    def verify_files(path):
        manifest = json.loads((path / "snapshot_manifest.json").read_text(encoding="utf-8"))
        for name, expected in manifest["files"].items():
            candidate = (path / name).resolve()
            if candidate.parent != path.resolve() or sha256_file(candidate) != expected:
                raise SnapshotError("snapshot_integrity_failed")
        return manifest
