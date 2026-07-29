from __future__ import annotations

import json
from pathlib import Path

import pytest

from wiki_review_v2.errors import OutputConflictError, classify_exception
from wiki_review_v2.fixtures import DocumentExtractor
from wiki_review_v2.models import DocumentBlock, InputCoverage, SourceDocument, VisualManifest
from wiki_review_v2.notifications import NotificationRenderer
from wiki_review_v2.model import strict_json_schema
from wiki_review_v2.prompts import ReviewPromptBuilder
from wiki_review_v2.storage import atomic_write_json


def test_extractor_preserves_markdown_table() -> None:
    result = DocumentExtractor().extract(
        [DocumentBlock(type="table", markdown="|a|b|\n|-|-|\n|1|2|")]
    )
    assert "【表格】" in result["content_markdown"]
    assert "|1|2|" in result["content_markdown"]
    assert result["table_count"] == 1


def test_prompt_has_composable_sections_and_no_base64() -> None:
    prompt = ReviewPromptBuilder("标准").build(
        source=SourceDocument(
            case_id="case",
            document_id="d",
            node_token="node",
            title="t",
            wiki_name="wiki",
            author_id="author-id",
            author="a",
            link="https://example.invalid/wiki/d",
        ),
        content="正文",
        manifest=VisualManifest(source="unavailable"),
        coverage=InputCoverage(structured_text_available=True, visual_pages_complete=False),
        review_mode="initial",
        similarity_context={},
        rereview_context={},
        attachments=[],
    )
    for heading in (
        "SystemRole",
        "ReviewStandard",
        "InputSemantics",
        "StructuredContent",
        "VisualInputGuide",
        "VisualManifest",
        "AttachmentGuide",
        "InputCoverageGuide",
        "DecisionRules",
        "OutputRequirements",
    ):
        assert f"## {heading}" in prompt
    assert ";base64," not in prompt
    assert "Authorization" not in prompt
    assert "Bearer" not in prompt
    assert "API Key" not in prompt


def test_atomic_write_refuses_non_identical_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    atomic_write_json(path, {"value": 1})
    atomic_write_json(path, {"value": 1}, allow_identical=True)
    with pytest.raises(OutputConflictError):
        atomic_write_json(path, {"value": 2})
    assert json.loads(path.read_text(encoding="utf-8"))["value"] == 1


def test_exception_classification_does_not_include_raw_detail() -> None:
    classified = classify_exception(RuntimeError("Authorization: Bearer secret"))
    assert "secret" not in json.dumps(classified)


def test_strict_json_schema_requires_every_object_property() -> None:
    schema = strict_json_schema({"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "array", "items": {"type": "object", "properties": {"c": {"type": "integer"}}}}}})
    assert schema["required"] == ["a", "b"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["b"]["items"]["required"] == ["c"]
