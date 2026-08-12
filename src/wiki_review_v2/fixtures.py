from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .errors import FixtureError
from .models import (
    DocumentBlock,
    FixtureOptions,
    FixtureBundle,
    PreviousIssue,
    PreviousReview,
    SimilarityCandidate,
    SourceDocument,
)


def _read_json(path: Path, *, required: bool, default: Any) -> Any:                    #读取json文件
    if not path.exists():
        if required:
            raise FixtureError(f"Fixture 缺少文件：{path.name}")
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureError(f"Fixture JSON 无法读取：{path.name}") from exc


class FixtureDocumentSource:
    def list_cases(self, root: Path) -> list[str]:                                     #列出案例
        if not root.exists():
            return []
        return sorted(path.name for path in root.iterdir() if path.is_dir() and (path / "source_document.json").exists())

    def load(self, case_path: Path) -> FixtureBundle:                                          #读取器
        if not case_path.is_dir():
            raise FixtureError(f"Fixture 不存在：{case_path}")
        try:
            source = SourceDocument.model_validate(_read_json(case_path / "source_document.json", required=True, default={}))
            blocks_raw = _read_json(case_path / "document_blocks.json", required=True, default=[])
            blocks = blocks_raw.get("blocks", []) if isinstance(blocks_raw, dict) else blocks_raw
            if not isinstance(blocks, list) or not all(isinstance(item, dict) for item in blocks):
                raise FixtureError("document_blocks.json 必须包含 blocks 数组")
            attachments_raw = _read_json(case_path / "attachment_metadata.json", required=False, default=[])
            attachments = attachments_raw.get("attachments", []) if isinstance(attachments_raw, dict) else attachments_raw
            if not isinstance(attachments, list):
                raise FixtureError("attachment_metadata.json 的 attachments 必须为数组")
            candidates_raw = _read_json(case_path / "similarity_candidates.json", required=False, default=[])
            if isinstance(candidates_raw, dict):
                candidates_raw = candidates_raw.get("similar_documents", candidates_raw.get("candidates", []))
            candidates = [
                SimilarityCandidate.model_validate(item)
                for item in candidates_raw
            ]
            previous_raw = _read_json(case_path / "previous_review.json", required=False, default=None)
            previous = self._previous_review(source, previous_raw)
            options = FixtureOptions.model_validate(
                _read_json(case_path / "fixture_options.json", required=False, default={})
            )
            mock_path = case_path / "mock_llm_result.json"
            if not mock_path.exists():
                mock_path = case_path / "fake_model_response.json"
            fake = self._adapt_mock(_read_json(mock_path, required=False, default={}))
            expected = _read_json(case_path / "expected_result.json", required=False, default={})
            return FixtureBundle(
                source_document=source,
                blocks=blocks,
                attachments=attachments,
                similarity_candidates=candidates,
                previous_review=previous,
                fixture_options=options,
                fake_model_response=fake,
                expected_result=expected,
            )
        except ValidationError as exc:
            raise FixtureError("Fixture 字段不符合 Schema") from exc

    @staticmethod
    def _adapt_mock(raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict) or "behavior" not in raw:
            return raw if isinstance(raw, dict) else {}
        behavior = raw.get("behavior")
        adapted: dict[str, Any]
        if behavior == "return":
            adapted = {"review": raw.get("response")}
        elif behavior == "raw":
            adapted = {"raw": raw.get("raw_output", "")}
        elif behavior in {"raise", "error"}:
            adapted = {"raise": raw.get("error_type", raw.get("error", "api"))}
        else:
            adapted = {}
        if isinstance(raw.get("visual_batches"), list):
            adapted["visual_batches"] = raw["visual_batches"]
        return adapted

    @staticmethod
    def _previous_review(source: SourceDocument, raw: Any) -> PreviousReview | None:
        if raw:
            return PreviousReview.model_validate(raw)
        if not source.previous_issues:
            return None
        issues: list[PreviousIssue] = []
        for index, item in enumerate(source.previous_issues, 1):
            if not isinstance(item, dict):
                continue
            issues.append(
                PreviousIssue.model_validate(
                    {
                        "issue_id": item.get("issue_id") or f"previous-{index}",
                        "level": item.get("level", "major"),
                        "category": item.get("category", "other"),
                        "position": item.get("position", ""),
                        "problem": item.get("problem", ""),
                        "suggestion": item.get("suggestion", ""),
                        "evidence_ids": item.get("evidence_ids", []),
                    }
                )
            )
        return PreviousReview(
            document_id=source.document_id,
            review_round=max(source.review_round, 1),
            result="need_revision",
            issues=issues,
        )


class DocumentExtractor:
    def extract(self, blocks: list[DocumentBlock | dict[str, Any]]) -> dict[str, Any]:                #提取器
        parts: list[str] = []
        table_count = 0
        image_count = 0
        for raw_block in blocks:
            if isinstance(raw_block, dict) and "block_type" in raw_block:
                rendered, is_table, is_image = self._render_v1_block(raw_block, image_count + 1)
                if rendered:
                    parts.append(rendered)
                table_count += int(is_table)
                image_count += int(is_image)
                continue
            block = raw_block if isinstance(raw_block, DocumentBlock) else DocumentBlock.model_validate(raw_block)
            if block.type == "heading":
                level = min(max(block.level or 1, 1), 6)
                parts.append(f"{'#' * level} {block.text.strip()}")
            elif block.type == "code":
                parts.append(f"```{block.language or ''}\n{block.text.rstrip()}\n```")
            elif block.type == "table":
                table_count += 1
                table = (block.markdown or block.text).strip()
                parts.append(f"【表格】\n{table}")
            elif block.type == "image":
                image_count += 1
                label = block.caption or block.text or f"图片 {image_count}"
                parts.append(f"[图片说明：{label.strip()}]")
            elif block.type == "quote":
                parts.append("\n".join(f"> {line}" for line in block.text.splitlines()))
            elif block.type == "list":
                parts.append("\n".join(f"- {line.strip()}" for line in block.text.splitlines() if line.strip()))
            else:
                parts.append(block.text.strip())
        content = "\n\n".join(item for item in parts if item).strip()
        return {
            "schema_version": "2.0",
            "content_markdown": content,
            "character_count": len(content),
            "block_count": len(blocks),
            "table_count": table_count,
            "image_count": image_count,
        }

    def _render_v1_block(self, block: dict[str, Any], image_number: int) -> tuple[str, bool, bool]:
        block_type = str(block.get("block_type") or "")
        if block_type == "table":
            return self._render_v1_table(block), True, False
        if block_type == "image":
            caption = self._collect_text(block) or f"图片 {image_number}"
            return f"[图片说明：{caption}]", False, True
        content = self._collect_text(block).strip()
        if not content:
            return "", False, False
        if block_type.startswith("heading"):
            suffix = block_type.removeprefix("heading")
            level = int(suffix) if suffix.isdigit() else 1
            return f"{'#' * min(max(level, 1), 6)} {content}", False, False
        if block_type == "code":
            language = str((block.get("code") or {}).get("language") or "")
            return f"```{language}\n{content}\n```", False, False
        if block_type in {"bullet", "ordered"}:
            marker = "-" if block_type == "bullet" else "1."
            return f"{marker} {content}", False, False
        return content, False, False

    def _render_v1_table(self, block: dict[str, Any]) -> str:
        column_size = int((block.get("table") or {}).get("column_size") or 0)
        cells: dict[tuple[int, int], str] = {}
        max_row = -1
        for child in block.get("children") or []:
            if not isinstance(child, dict) or child.get("block_type") != "table_cell":
                continue
            location = (child.get("table_cell") or {}).get("location") or {}
            row = int(location.get("row", 0))
            column = int(location.get("column", 0))
            max_row = max(max_row, row)
            cells[(row, column)] = self._collect_text(child).strip().replace("\n", "<br>")
        if column_size <= 0:
            column_size = max((column for _, column in cells), default=-1) + 1
        if column_size <= 0 or max_row < 0:
            return "【表格】"
        rows = [[cells.get((row, column), "") for column in range(column_size)] for row in range(max_row + 1)]
        header = rows[0]
        body = rows[1:]
        markdown = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join("---" for _ in range(column_size)) + " |",
            *("| " + " | ".join(row) + " |" for row in body),
        ]
        return "【表格】\n" + "\n".join(markdown)

    def _collect_text(self, value: Any) -> str:
        pieces: list[str] = []

        def visit(item: Any) -> None:
            if isinstance(item, dict):
                text_run = item.get("text_run")
                if isinstance(text_run, dict) and isinstance(text_run.get("content"), str):
                    pieces.append(text_run["content"])
                    return
                for key, child in item.items():
                    if key not in {"table", "table_cell", "image", "location"}:
                        visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)

        visit(value)
        return "".join(pieces)
