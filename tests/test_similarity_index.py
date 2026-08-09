from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from wiki_review_v2.errors import SimilarityIndexError
from wiki_review_v2.models import SimilarityProfile, SourceDocument
from wiki_review_v2.similarity_index import (
    IndexedSimilarityArticle,
    LocalSimilarityRetriever,
    SimilarityIndexStore,
    TextSimilarityScorer,
)


def _source(document_id: str, title: str = "无人机飞控硬件") -> SourceDocument:
    return SourceDocument(
        case_id=document_id,
        document_id=document_id,
        node_token=f"node-{document_id}",
        title=title,
        wiki_name="历史知识库",
        author_id="author",
        author="作者",
        link=f"https://example.invalid/{document_id}",
        status="AI审稿中",
        updated_at="2026/08/08 09:00:00",
    )


def _profile(document_id: str, *, title: str = "无人机飞控硬件", content: str | None = None) -> SimilarityProfile:
    summary = content or (
        "标题：无人机飞控硬件\n章节主题：电源与通信、台架测试\n"
        "关键词：无人机、飞控、电源、测试\n技术实体：STM32H743、ICM-42688-P、CAN-FD\n"
        "代表内容：使用 STM32H743 和 ICM-42688-P，通过 CAN-FD 完成 24 V 电源台架验证。"
    )
    return SimilarityProfile(
        document_id=document_id,
        title=title,
        source="blocks",
        content=summary,
        headings=["电源与通信", "台架测试"],
        keywords=["无人机", "飞控", "电源", "测试"],
        technical_entities=["STM32H743", "ICM-42688-P", "CAN-FD"],
        parameters=["24 V"],
        source_character_count=len(summary),
        summary_character_count=len(summary),
    )


def _indexed(document_id: str, *, unrelated: bool = False) -> IndexedSimilarityArticle:
    if unrelated:
        return IndexedSimilarityArticle(
            document_id=document_id,
            title="烘焙配方记录",
            wiki_name="生活库",
            node_token="node",
            link="https://example.invalid/bread",
            content="标题：烘焙配方记录\n关键词：面粉、酵母、烤箱\n代表内容：记录面包发酵温度和烘烤时间。",
            keywords=["面粉", "酵母", "烤箱"],
            technical_entities=["OVEN"],
            source="blocks",
            review_result="pass",
            local_status="AI通过待确认",
            source_updated_at="",
            indexed_at="",
        )
    profile = _profile(document_id)
    return IndexedSimilarityArticle(
        document_id=document_id,
        title=profile.title,
        wiki_name="历史知识库",
        node_token="node",
        link=f"https://example.invalid/{document_id}",
        content=profile.content,
        keywords=profile.keywords,
        technical_entities=profile.technical_entities,
        source="blocks",
        review_result="pass",
        local_status="AI通过待确认",
        source_updated_at="",
        indexed_at="",
    )


def test_index_auto_create_upsert_and_exclude_self(tmp_path: Path) -> None:
    store = SimilarityIndexStore(tmp_path / "new-folder" / "articles.sqlite")
    assert store.info()["record_count"] == 0
    store.upsert(
        source=_source("doc-a"),
        profile=_profile("doc-a"),
        review_result="pass",
        local_status="AI通过待确认",
    )
    assert store.info()["record_count"] == 1
    assert store.query(exclude_document_id="doc-a") == []

    updated_profile = _profile("doc-a", title="更新后的标题")
    store.upsert(
        source=_source("doc-a", "更新后的标题"),
        profile=updated_profile,
        review_result="pass",
        local_status="AI通过待确认",
    )
    rows = store.query(exclude_document_id="other")
    assert len(rows) == 1 and rows[0].title == "更新后的标题"
    with pytest.raises(SimilarityIndexError):
        store.upsert(
            source=_source("bad"),
            profile=_profile("bad"),
            review_result="need_revision",
            local_status="需修改",
        )


def test_index_schema_stores_only_summary_not_historical_files(tmp_path: Path) -> None:
    store = SimilarityIndexStore(tmp_path / "articles.sqlite")
    store.initialize()
    with sqlite3.connect(store.path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(similarity_articles)")}
    assert "content" in columns
    assert not {"full_text", "pdf_path", "png_path", "image_path"} & columns


def test_index_text_fields_do_not_contain_request_secrets(tmp_path: Path) -> None:
    store = SimilarityIndexStore(tmp_path / "articles.sqlite")
    store.upsert(
        source=_source("safe"),
        profile=_profile("safe"),
        review_result="pass",
        local_status="AI通过待确认",
    )
    with sqlite3.connect(store.path) as connection:
        row = connection.execute("SELECT * FROM similarity_articles").fetchone()
    serialized = " ".join(str(value) for value in row)
    for forbidden in ("Authorization", "Bearer ", ";base64,", "test-secret-key"):
        assert forbidden not in serialized


def test_new_environment_names_and_legacy_aliases(tmp_path: Path, monkeypatch) -> None:
    from wiki_review_v2.config import Settings

    monkeypatch.setenv("SIMILARITY_THRESHOLD", "0.42")
    monkeypatch.setenv("SIMILARITY_TOP_K", "7")
    monkeypatch.setenv("SIMILARITY_SUMMARY_MAX_CHARS", "1000")
    monkeypatch.setenv("SIMILARITY_INDEX_PATH", "custom-index/articles.sqlite")
    configured = Settings.from_env(tmp_path)
    assert configured.similarity_threshold == 0.42
    assert configured.similarity_top_k == 7
    assert configured.similarity_summary_max_chars == 1000
    assert configured.similarity_index_path == tmp_path / "custom-index" / "articles.sqlite"
    assert configured.similarity_min_score == 0.42
    assert configured.similarity_top_n == 7


def test_similar_chinese_text_scores_higher_and_formula_is_exact() -> None:
    scorer = TextSimilarityScorer()
    current = _profile("current")
    scored = scorer.score_all(current, [_indexed("similar"), _indexed("unrelated", unrelated=True)])
    similar = scored[0][1]
    unrelated = scored[1][1]
    assert similar.final_score > unrelated.final_score
    assert similar.text_tfidf > unrelated.text_tfidf
    expected = (
        0.70 * similar.text_tfidf
        + 0.15 * similar.title_similarity
        + 0.10 * similar.keyword_jaccard
        + 0.05 * similar.entity_jaccard
    )
    assert similar.final_score == pytest.approx(expected, abs=1e-6)


def test_threshold_top_k_and_stable_sort(settings, tmp_path: Path) -> None:
    configured = settings.model_copy(update={"similarity_threshold": 0.0, "similarity_top_k": 2})
    store = SimilarityIndexStore(tmp_path / "articles.sqlite")
    for document_id in ("beta", "alpha", "gamma"):
        store.upsert(
            source=_source(document_id),
            profile=_profile(document_id),
            review_result="pass",
            local_status="AI通过待确认",
        )
    selected, audit = LocalSimilarityRetriever(configured, store).retrieve(
        current_document_id="current", current_profile=_profile("current")
    )
    assert [item.document_id for item in selected] == ["alpha", "beta"]
    assert len(audit.scored_candidates) == 3

    strict = configured.model_copy(update={"similarity_threshold": 1.0})
    selected, audit = LocalSimilarityRetriever(strict, store).retrieve(
        current_document_id="current",
        current_profile=_profile("current", content="完全不相关的当前文章摘要，仅讨论植物生长。"),
    )
    assert selected == []
    assert all(not item.above_threshold for item in audit.scored_candidates)
