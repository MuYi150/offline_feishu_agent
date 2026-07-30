from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET
from zipfile import ZipFile


NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
}
REL_ID = f"{{{NS['r']}}}embed"
RELATIONSHIP_ID = "Id"
RELATIONSHIP_TARGET = "Target"
STYLE_VALUE = f"{{{NS['w']}}}val"


def _json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _binary_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(value)
    os.replace(temporary, path)


def _text_elements(content: str) -> list[dict[str, Any]]:
    return [{"text_run": {"content": content}}]


def _text_block(block_type: str, content: str) -> dict[str, Any]:
    return {block_type: {"elements": _text_elements(content)}, "block_type": block_type}


def _paragraph_kind(text: str, style: str, *, is_title: bool) -> tuple[str, str]:
    normalized_style = style.lower().replace(" ", "")
    heading_match = re.match(r"heading([1-6])$", normalized_style)
    if heading_match:
        return f"heading{heading_match.group(1)}", text
    if is_title or re.match(r"^[一二三四五六七八九十百]+、", text):
        return "heading1", text
    if re.match(r"^\d+\.\d+(?:\.\d+)?\s+", text):
        return "heading2", text
    ordered = re.match(r"^\d+[.、]\s*(.+)$", text)
    if ordered:
        return "ordered", ordered.group(1)
    return "text", text


def _relationship_targets(archive: ZipFile) -> dict[str, str]:
    root = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
    return {
        item.attrib[RELATIONSHIP_ID]: str(PurePosixPath("word") / item.attrib[RELATIONSHIP_TARGET])
        for item in root.findall("pr:Relationship", NS)
        if "media/" in item.attrib.get(RELATIONSHIP_TARGET, "")
    }


def _paragraph_blocks(
    paragraph: ET.Element,
    relationships: dict[str, str],
    image_tokens: dict[str, str],
    *,
    is_title: bool,
) -> list[dict[str, Any]]:
    text = "".join(node.text or "" for node in paragraph.findall(".//w:t", NS)).strip()
    style_node = paragraph.find("./w:pPr/w:pStyle", NS)
    style = style_node.attrib.get(STYLE_VALUE, "") if style_node is not None else ""
    blocks: list[dict[str, Any]] = []
    if text:
        block_type, content = _paragraph_kind(text, style, is_title=is_title)
        blocks.append(_text_block(block_type, content))
    for blip in paragraph.findall(".//a:blip", NS):
        relationship_id = blip.attrib.get(REL_ID, "")
        target = relationships.get(relationship_id)
        if target and target in image_tokens:
            blocks.append(
                {
                    "block_type": "image",
                    "image": {"token": image_tokens[target]},
                }
            )
    return blocks


def _table_block(table: ET.Element) -> dict[str, Any]:
    children: list[dict[str, Any]] = []
    rows = table.findall("./w:tr", NS)
    column_size = max((len(row.findall("./w:tc", NS)) for row in rows), default=0)
    for row_index, row in enumerate(rows):
        for column_index, cell in enumerate(row.findall("./w:tc", NS)):
            content = "\n".join(
                text
                for paragraph in cell.findall(".//w:p", NS)
                if (text := "".join(node.text or "" for node in paragraph.findall(".//w:t", NS)).strip())
            )
            children.append(
                {
                    "block_type": "table_cell",
                    "table_cell": {"location": {"row": row_index, "column": column_index}},
                    "children": [_text_block("text", content)] if content else [],
                }
            )
    return {"block_type": "table", "table": {"column_size": column_size}, "children": children}


def import_docx(source: Path, output: Path, *, case_id: str, force: bool) -> None:
    required_outputs = [
        "source_document.json",
        "document_blocks.json",
        "attachment_metadata.json",
        "similarity_candidates.json",
        "mock_llm_result.json",
        "expected_result.json",
        "image_manifest.json",
    ]
    conflicts = [name for name in required_outputs if (output / name).exists()]
    if conflicts and not force:
        raise SystemExit(f"refusing to overwrite existing fixture files: {', '.join(conflicts)}")

    with ZipFile(source) as archive:
        relationships = _relationship_targets(archive)
        media_parts = sorted(name for name in archive.namelist() if name.startswith("word/media/"))
        image_tokens = {name: f"fixture_image_{index:03d}" for index, name in enumerate(media_parts, 1)}
        document = ET.fromstring(archive.read("word/document.xml"))
        body = document.find("w:body", NS)
        if body is None:
            raise SystemExit("DOCX does not contain word/document.xml body")

        blocks: list[dict[str, Any]] = []
        title = source.stem
        title_consumed = False
        image_locations: dict[str, list[int]] = {name: [] for name in media_parts}
        for paragraph_index, child in enumerate(body):
            if child.tag == f"{{{NS['w']}}}p":
                text = "".join(node.text or "" for node in child.findall(".//w:t", NS)).strip()
                if text and not title_consumed:
                    title = text
                paragraph_blocks = _paragraph_blocks(
                    child,
                    relationships,
                    image_tokens,
                    is_title=bool(text and not title_consumed),
                )
                for blip in child.findall(".//a:blip", NS):
                    target = relationships.get(blip.attrib.get(REL_ID, ""))
                    if target in image_locations:
                        image_locations[target].append(paragraph_index)
                blocks.extend(paragraph_blocks)
                title_consumed = title_consumed or bool(text)
            elif child.tag == f"{{{NS['w']}}}tbl":
                blocks.append(_table_block(child))

        images: list[dict[str, Any]] = []
        for media_part in media_parts:
            data = archive.read(media_part)
            filename = PurePosixPath(media_part).name
            target = output / "images" / filename
            _binary_write(target, data)
            images.append(
                {
                    "token": image_tokens[media_part],
                    "path": f"images/{filename}",
                    "source_part": media_part,
                    "byte_size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "paragraph_indexes": image_locations[media_part],
                }
            )

    document_id = f"test_doc_{case_id}"
    page_count = len(list((output / "pages").glob("page-*.png")))
    updated_at = datetime.fromtimestamp(source.stat().st_mtime).strftime("%Y/%m/%d %H:%M:%S")
    source_document = {
        "case_id": case_id,
        "document_id": document_id,
        "node_token": f"test_node_{case_id}",
        "title": title,
        "wiki_name": "离线测试知识库",
        "author_id": "fixture_docx_importer",
        "author": "DOCX Fixture 导入",
        "link": f"https://example.invalid/wiki/{document_id}",
        "review_method": "AI",
        "status": "AI审稿中",
        "review_round": 0,
        "updated_at": updated_at,
        "last_ai_review_at": "",
    }
    mock_response = {
        "behavior": "return",
        "response": {
            "result": "pass",
            "summary": "文档包含完整的硬件设计说明、视觉材料、测试计划和迭代方向。",
            "pass_reason": "结构化正文与硬件设计图相互印证，达到知识库设计类研发文档的审稿标准。",
            "blocking_count": 0,
            "major_count": 0,
            "minor_count": 0,
            "issues": [],
            "similarity_check": {
                "status": "no_similar",
                "decision": "keep_independent",
                "summary": "本 Fixture 未提供有效相似候选。",
                "candidate_count": 0,
                "candidates_considered": [],
            },
            "learning_trace_assessment": "文档描述了硬件方案、测试要点和迭代方向，但没有提供测试数据，且文末声明部分内容可能由 AI 生成。",
            "visual_evidence_assessment": {
                "coverage": "complete",
                "pages_reviewed": list(range(1, page_count + 1)),
                "evidence_used": [
                    {
                        "evidence_id": "page-3",
                        "page": 3,
                        "observation": "页面包含无人机主控板 3D 渲染图。",
                        "supports": ["hardware_layout"],
                    },
                    {
                        "evidence_id": "page-5",
                        "page": 5,
                        "observation": "页面包含 PCB 布线工程图。",
                        "supports": ["pcb_routing"],
                    },
                ]
                if page_count >= 5
                else [],
                "limitations": [],
            },
            "revision_priority": [],
            "suggested_next_action": "admin_confirm",
            "re_review_assessment": {"resolutions": []},
        },
    }
    expected_result = {
        "exit_code": 0,
        "result": "pass",
        "table_status": "AI通过待确认",
        "review_mode": "initial_review",
        "review_round": 1,
        "ocr_success": False,
        "table_count": sum(1 for block in blocks if block.get("block_type") == "table"),
        "failure_stage": "",
        "notifications_generated": True,
        "modified_document_detected": False,
    }

    _json_write(output / "source_document.json", source_document)
    _json_write(output / "document_blocks.json", {"blocks": blocks})
    _json_write(output / "attachment_metadata.json", {"document_id": document_id, "attachments": []})
    _json_write(output / "similarity_candidates.json", {"document_id": document_id, "similar_documents": []})
    _json_write(
        output / "image_manifest.json",
        {
            "schema_version": "2.0",
            "source_document": source.name,
            "image_count": len(images),
            "images": images,
        },
    )
    _json_write(output / "mock_llm_result.json", mock_response)
    _json_write(output / "expected_result.json", expected_result)
    print(f"fixture={output.resolve()}")
    print(f"blocks={len(blocks)} images={len(images)} pages={page_count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Import a DOCX into a v1-compatible WIKI_V2 fixture")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if not args.source.is_file():
        raise SystemExit(f"DOCX not found: {args.source}")
    import_docx(args.source, args.output, case_id=args.case_id, force=args.force)


if __name__ == "__main__":
    main()
