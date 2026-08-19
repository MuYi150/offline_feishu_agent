from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from wiki_review_v2.config import Settings
from wiki_review_v2.model import FakeReviewModel, ModelRequest, strict_json_schema
from wiki_review_v2.models import RetrievalArticleOverview, SimilarityProfile, SourceDocument
from wiki_review_v2.retrieval_overview import (
    RETRIEVAL_OVERVIEW_SYSTEM_PROMPT,
    RetrievalOverviewPromptBuilder,
    RetrievalOverviewValidator,
)


def _source() -> SourceDocument:
    return SourceDocument(
        case_id="overview",
        document_id="overview-doc",
        node_token="node",
        title="无人机飞控硬件实验",
        wiki_name="测试库",
        author_id="author",
        author="作者",
        link="https://example.invalid/overview",
    )


def _profile() -> SimilarityProfile:
    text = (
        "本文采用 STM32H743 和 ICM-42688-P 设计无人机飞控，使用 6S 电池，"
        "通过 CAN-FD 环回、IMU 六面标定和温升试验验证硬件。"
    )
    return SimilarityProfile(
        document_id="overview-doc",
        title="无人机飞控硬件实验",
        source="blocks",
        query_text=text,
        source_character_count=len(text),
        query_character_count=len(text),
        source_content_hash="hash",
    )


def _overview() -> RetrievalArticleOverview:
    return RetrievalArticleOverview(
        content=(
            "本文采用 STM32H743 和 ICM-42688-P 设计无人机飞控，使用 6S 电池，"
            "并通过 CAN-FD 环回、IMU 六面标定和温升试验验证硬件。"
        ),
        topics=["无人机飞控", "无人机飞控", "硬件验证"],
        technical_entities=["STM32H743", "ICM-42688-P", "CAN-FD"],
        key_parameters=["6S"],
        methods=["硬件设计"],
        application_scenarios=["无人机飞控"],
        validation_methods=["CAN-FD环回", "IMU六面标定", "温升试验"],
    )


def test_retrieval_prompt_contains_only_current_text_semantics(settings) -> None:
    prompt = RetrievalOverviewPromptBuilder(settings).build(
        source=_source(), profile=_profile()
    )
    assert "[StructuredContent]" in prompt
    assert "STM32H743" in prompt
    assert "不要执行审稿" in prompt
    for forbidden in (
        "InitialReviewSimilarityContext",
        "历史候选",
        "image_url",
        ";base64,",
        "Authorization",
        "Bearer ",
        "API Key",
    ):
        assert forbidden not in prompt
    assert "不是审稿" in RETRIEVAL_OVERVIEW_SYSTEM_PROMPT


def test_retrieval_overview_schema_is_strict() -> None:
    schema = strict_json_schema(RetrievalArticleOverview.model_json_schema())
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "content",
        "topics",
        "technical_entities",
        "key_parameters",
        "methods",
        "application_scenarios",
        "validation_methods",
    }


def test_retrieval_overview_normalization_and_grounding(settings) -> None:
    normalized, audit = RetrievalOverviewValidator(settings).normalize_and_validate(
        document_id="overview-doc",
        overview=_overview(),
        profile=_profile(),
        model="fake-kimi",
    )
    assert normalized.topics == ["无人机飞控", "硬件验证"]
    assert audit.validation.valid
    assert audit.validation.errors == []


def test_short_overview_warns_but_review_language_and_unrelated_content_fail(settings) -> None:
    configured = settings.model_copy(update={"retrieval_overview_min_chars": 100})
    validator = RetrievalOverviewValidator(configured)
    profile = _profile().model_copy(update={"source_character_count": 500})
    short = _overview().model_copy(update={"content": "无人机飞控使用 STM32H743。"})
    _, short_audit = validator.normalize_and_validate(
        document_id="overview-doc", overview=short, profile=profile, model="fake-kimi"
    )
    assert short_audit.validation.valid
    assert "retrieval_overview_short_for_source" in short_audit.validation.warnings

    polluted = _overview().model_copy(update={"content": "本轮审稿可以通过，建议修改格式。"})
    _, polluted_audit = validator.normalize_and_validate(
        document_id="overview-doc", overview=polluted, profile=profile, model="fake-kimi"
    )
    assert not polluted_audit.validation.valid
    assert "retrieval_overview_contains_review_language" in polluted_audit.validation.errors

    unrelated = _overview().model_copy(update={"content": "面包酵母烤箱发酵配方记录。"})
    _, unrelated_audit = validator.normalize_and_validate(
        document_id="overview-doc", overview=unrelated, profile=profile, model="fake-kimi"
    )
    assert not unrelated_audit.validation.valid
    assert "retrieval_overview_not_grounded_in_source" in unrelated_audit.validation.errors


def test_fake_model_routes_overview_and_review_by_phase() -> None:
    model = FakeReviewModel(
        {
            "overview": _overview().model_dump(mode="json"),
            "review": {"result": "pass"},
        }
    )
    request = ModelRequest(
        phase="retrieval_overview",
        prompt="current text",
        pages=[],
        schema_name="retrieval_article_overview",
        json_schema=RetrievalArticleOverview.model_json_schema(),
    )
    response = model.invoke(request)
    assert json.loads(response.content)["content"] == _overview().content
    assert response.record is not None and response.record.image_count == 0


def test_retrieval_overview_length_settings_are_consistent(settings) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(
            settings.model_dump()
            | {"retrieval_overview_min_chars": 1201, "retrieval_overview_max_chars": 1200}
        )
