from __future__ import annotations

import json

from wiki_review_v2.fixtures import FixtureDocumentSource


V1_SOURCE_KEYS = {
    "case_id",
    "document_id",
    "node_token",
    "title",
    "wiki_name",
    "author_id",
    "author",
    "link",
    "review_method",
    "status",
    "review_round",
    "updated_at",
    "last_ai_review_at",
}


def test_generated_basic_source_uses_exact_v1_shape(settings) -> None:
    case = settings.fixtures_root / "basic_pass"
    raw = json.loads((case / "source_document.json").read_text(encoding="utf-8"))
    assert set(raw) == V1_SOURCE_KEYS
    assert raw["case_id"] == "basic_pass"
    assert raw["title"] == "笔记_Python基础实践-v1.0"
    assert raw["author_id"] == "test_user_001"
    assert raw["author"] == "测试用户001"
    assert raw["review_round"] == 0
    assert raw["status"] == "AI审稿中"


def test_all_generated_sources_keep_v1_base_shape(settings) -> None:
    for case in settings.fixtures_root.iterdir():
        if not case.is_dir():
            continue
        raw = json.loads((case / "source_document.json").read_text(encoding="utf-8"))
        assert V1_SOURCE_KEYS <= set(raw), case.name
        assert set(raw) - V1_SOURCE_KEYS <= {"previous_issues"}, case.name
        options_path = case / "fixture_options.json"
        options = json.loads(options_path.read_text(encoding="utf-8")) if options_path.exists() else {}
        if not options.get("real_model_only"):
            assert (case / "mock_llm_result.json").is_file(), case.name
        assert not (case / "fake_model_response.json").exists(), case.name


def test_blocks_attachments_mock_and_expected_keep_v1_wrappers(settings) -> None:
    case = settings.fixtures_root / "multimodal_pass"
    blocks = json.loads((case / "document_blocks.json").read_text(encoding="utf-8"))
    attachments = json.loads((case / "attachment_metadata.json").read_text(encoding="utf-8"))
    mock = json.loads((case / "mock_llm_result.json").read_text(encoding="utf-8"))
    expected = json.loads((case / "expected_result.json").read_text(encoding="utf-8"))
    assert set(blocks) == {"blocks"}
    assert all("block_type" in block for block in blocks["blocks"])
    assert set(attachments) == {"document_id", "attachments"}
    assert mock["behavior"] == "return" and "response" in mock
    assert {
        "exit_code",
        "result",
        "table_status",
        "review_mode",
        "review_round",
        "ocr_success",
        "table_count",
        "failure_stage",
        "notifications_generated",
        "modified_document_detected",
    } == set(expected)


def test_loader_derives_rereview_history_from_v1_source(settings) -> None:
    bundle = FixtureDocumentSource().load(settings.fixtures_root / "rereview_resolved")
    assert bundle.source_document.review_round == 1
    assert bundle.previous_review is not None
    assert [issue.level.value for issue in bundle.previous_review.issues] == ["major", "minor"]
    assert bundle.fake_model_response["review"]["result"] == "pass"


def test_v1_raw_table_blocks_are_extracted_as_markdown(settings) -> None:
    source = FixtureDocumentSource()
    bundle = source.load(settings.fixtures_root / "multimodal_pass")
    from wiki_review_v2.fixtures import DocumentExtractor

    extracted = DocumentExtractor().extract(bundle.blocks)
    assert extracted["table_count"] == 1
    assert "【表格】" in extracted["content_markdown"]
    assert "| 配置 | 值 |" in extracted["content_markdown"]
