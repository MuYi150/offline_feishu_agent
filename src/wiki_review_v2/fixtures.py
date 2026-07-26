from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .errors import FixtureError
from .models import (
    DocumentBlock,
    FixtureBundle,
    PreviousReview,
    SimilarityCandidate,
    SourceDocument,
)


def _read_json(path: Path, *, required: bool, default: Any) -> Any:
    if not path.exists():
        if required:
            raise FixtureError(f"Fixture 缺少文件：{path.name}")
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureError(f"Fixture JSON 无法读取：{path.name}") from exc


class FixtureDocumentSource:
    def list_cases(self, root: Path) -> list[str]:
        if not root.exists():
            return []
        return sorted(path.name for path in root.iterdir() if path.is_dir() and (path / "source_document.json").exists())

    def load(self, case_path: Path) -> FixtureBundle:
        if not case_path.is_dir():
            raise FixtureError(f"Fixture 不存在：{case_path}")
        try:
            source = SourceDocument.model_validate(_read_json(case_path / "source_document.json", required=True, default={}))
            blocks_raw = _read_json(case_path / "document_blocks.json", required=True, default=[])
            blocks = [DocumentBlock.model_validate(item) for item in blocks_raw]
            attachments = _read_json(case_path / "attachment_metadata.json", required=False, default=[])
            candidates = [
                SimilarityCandidate.model_validate(item)
                for item in _read_json(case_path / "similarity_candidates.json", required=False, default=[])
            ]
            previous_raw = _read_json(case_path / "previous_review.json", required=False, default=None)
            previous = PreviousReview.model_validate(previous_raw) if previous_raw else None
            fake = _read_json(case_path / "fake_model_response.json", required=False, default={})
            expected = _read_json(case_path / "expected_result.json", required=False, default={})
            return FixtureBundle(
                source_document=source,
                blocks=blocks,
                attachments=attachments,
                similarity_candidates=candidates,
                previous_review=previous,
                fake_model_response=fake,
                expected_result=expected,
            )
        except ValidationError as exc:
            raise FixtureError("Fixture 字段不符合 Schema") from exc


class DocumentExtractor:
    def extract(self, blocks: list[DocumentBlock]) -> dict[str, Any]:
        parts: list[str] = []
        table_count = 0
        image_count = 0
        for block in blocks:
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

