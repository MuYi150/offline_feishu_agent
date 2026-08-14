from __future__ import annotations

from wiki_review_v2.article_overview import ArticleOverviewValidator
from wiki_review_v2.models import ArticleOverview, SimilarityProfile


def _profile() -> SimilarityProfile:
    text = (
        "标题：无人机飞控硬件\n正文：本文使用 STM32H743 和 CAN-FD 设计飞控，"
        "输入 24 V，并完成台架测试和结果记录。"
    )
    return SimilarityProfile(
        document_id="current",
        title="无人机飞控硬件",
        source="blocks",
        query_text=text,
        local_keywords=["无人机", "飞控"],
        local_technical_entities=["STM32H743", "CAN-FD"],
        local_key_parameters=["24 V"],
        source_character_count=600,
        query_character_count=len(text),
        source_content_hash="a" * 64,
    )


def test_overview_is_normalized_and_grounded(settings) -> None:
    overview, audit = ArticleOverviewValidator(settings).normalize_and_validate(
        document_id="current",
        overview=ArticleOverview(
            content="本文使用 STM32H743 和 CAN-FD 设计无人机飞控，并以 24 V 输入完成台架测试。",
            topics=["无人机飞控", " 无人机飞控 "],
            technical_entities=["STM32H743", "STM32H743", "CAN-FD"],
            key_parameters=["24 V", "24V"],
        ),
        review_summary="文章内容完整，可以通过。",
        profile=_profile(),
        candidates=[],
        overview_model="fake-kimi",
    )
    assert overview is not None
    assert overview.topics == ["无人机飞控"]
    assert overview.technical_entities == ["STM32H743", "CAN-FD"]
    assert overview.key_parameters == ["24 V"]
    assert audit.validation_status == "warning"  # 对较长原文而言概述偏短，但仍可入库。
    assert audit.source_content_hash == "a" * 64


def test_ungrounded_entity_or_parameter_blocks_indexing(settings) -> None:
    _, audit = ArticleOverviewValidator(settings).normalize_and_validate(
        document_id="current",
        overview=ArticleOverview(
            content="本文介绍无人机飞控设计。",
            topics=["飞控"],
            technical_entities=["STM32H999"],
            key_parameters=["48 V"],
        ),
        review_summary="需要修改。",
        profile=_profile(),
        candidates=[],
        overview_model="fake-kimi",
    )
    assert audit.validation_status == "invalid"
    assert "ungrounded_technical_entity:STM32H999" in audit.validation_warnings
    assert "ungrounded_key_parameter:48 V" in audit.validation_warnings


def test_review_language_and_candidate_contamination_are_invalid(settings) -> None:
    _, audit = ArticleOverviewValidator(settings).normalize_and_validate(
        document_id="current",
        overview=ArticleOverview(
            content="本轮审稿引用历史专用方案 HISTORY-DOC-1，审稿通过。",
            topics=["飞控"],
            technical_entities=[],
            key_parameters=[],
        ),
        review_summary="其他审稿总结。",
        profile=_profile(),
        candidates=[{"document_id": "HISTORY-DOC-1", "title": "历史专用方案"}],
        overview_model="fake-kimi",
    )
    assert audit.validation_status == "invalid"
    assert "article_overview_contains_review_process_language" in audit.validation_warnings
    assert any(item.startswith("candidate_document_id_contamination") for item in audit.validation_warnings)


def test_review_summary_cannot_be_reused_as_article_overview(settings) -> None:
    summary = "本文内容完整并记录了关键结果。"
    _, audit = ArticleOverviewValidator(settings).normalize_and_validate(
        document_id="current",
        overview=ArticleOverview(
            content=summary,
            topics=[],
            technical_entities=[],
            key_parameters=[],
        ),
        review_summary=summary,
        profile=_profile(),
        candidates=[],
        overview_model="fake-kimi",
    )
    assert audit.validation_status == "invalid"
    assert "article_overview_equals_review_summary" in audit.validation_warnings
