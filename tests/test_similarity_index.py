from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from wiki_review_v2.errors import SimilarityIndexError
from wiki_review_v2.models import RetrievalArticleOverview, SimilarityProfile, SourceDocument
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
    query = content or (
        "标题：无人机飞控硬件\n章节：电源与通信、台架测试\n"
        "关键词：无人机、飞控、电源、测试\n技术实体：STM32H743、ICM-42688-P、CAN-FD\n"
        "正文：使用 STM32H743 和 ICM-42688-P，通过 CAN-FD 完成 24 V 电源台架验证。"
    )
    return SimilarityProfile(
        document_id=document_id,
        title=title,
        source="blocks",
        query_text=query,
        headings=["电源与通信", "台架测试"],
        local_keywords=["无人机", "飞控", "电源", "测试"],
        local_technical_entities=["STM32H743", "ICM-42688-P", "CAN-FD"],
        local_key_parameters=["24 V"],
        source_character_count=len(query),
        query_character_count=len(query),
        source_content_hash=f"hash-{document_id}",
    )


def _overview(*, unrelated: bool = False) -> RetrievalArticleOverview:
    if unrelated:
        return RetrievalArticleOverview(
            content="本文记录面包发酵温度、酵母比例和烤箱烘烤时间。",
            topics=["面粉", "酵母", "烤箱"],
            technical_entities=["OVEN"],
            key_parameters=["180℃"],
            methods=["烘烤"],
            application_scenarios=["家庭烘焙"],
            validation_methods=["成品观察"],
        )
    return RetrievalArticleOverview(
        content="本文设计无人机飞控硬件，使用 STM32H743、ICM-42688-P 和 CAN-FD，并完成 24 V 电源台架测试。",
        topics=["无人机", "飞控", "电源", "测试"],
        technical_entities=["STM32H743", "ICM-42688-P", "CAN-FD"],
        key_parameters=["24 V"],
        methods=["硬件设计", "台架测试"],
        application_scenarios=["无人机飞控"],
        validation_methods=["电源台架测试"],
    )


def _indexed(document_id: str, *, unrelated: bool = False) -> IndexedSimilarityArticle:
    overview = _overview(unrelated=unrelated)
    return IndexedSimilarityArticle(
        document_id=document_id,
        title="烘焙配方记录" if unrelated else "无人机飞控硬件",
        wiki_name="生活库" if unrelated else "历史知识库",
        node_token="node",
        link=f"https://example.invalid/{document_id}",
        content=overview.content,
        topics=overview.topics,
        technical_entities=overview.technical_entities,
        key_parameters=overview.key_parameters,
        summary_source="ai_article_overview",
        overview_model="fake-kimi",
        overview_prompt_version="article-overview-v1",
        source_content_hash=f"hash-{document_id}",
        source="blocks",
        review_result="pass",
        local_status="AI通过待确认",
        source_updated_at="",
        indexed_at="",
        methods=overview.methods,
        application_scenarios=overview.application_scenarios,
        validation_methods=overview.validation_methods,
    )


def _upsert(store: SimilarityIndexStore, document_id: str, *, title: str = "无人机飞控硬件") -> None:
    profile = _profile(document_id, title=title)
    store.upsert(
        source=_source(document_id, title),
        profile=profile,
        article_overview=_overview(),
        overview_model="fake-kimi",
        source_content_hash=profile.source_content_hash,
        review_result="pass",
        local_status="AI通过待确认",
    )


def test_index_auto_create_upsert_and_exclude_self(tmp_path: Path) -> None:
    store = SimilarityIndexStore(tmp_path / "new-folder" / "articles.sqlite")
    assert store.info()["record_count"] == 0
    _upsert(store, "doc-a")
    assert store.info()["record_count"] == 1
    assert store.info()["summary_sources"] == {"ai_retrieval_overview": 1}
    assert store.query(exclude_document_id="doc-a") == []

    _upsert(store, "doc-a", title="更新后的标题")
    rows = store.query(exclude_document_id="other")
    assert len(rows) == 1 and rows[0].title == "更新后的标题"
    with pytest.raises(SimilarityIndexError):
        profile = _profile("bad")
        store.upsert(
            source=_source("bad"),
            profile=profile,
            article_overview=_overview(),
            overview_model="fake-kimi",
            source_content_hash=profile.source_content_hash,
            review_result="need_revision",
            local_status="需修改",
        )


def test_v1_index_is_migrated_without_deleting_legacy_rows(tmp_path: Path) -> None:
    path = tmp_path / "articles.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE similarity_index_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO similarity_index_metadata VALUES('schema_version','1')")
        connection.execute(
            """CREATE TABLE similarity_articles(
            document_id TEXT PRIMARY KEY,title TEXT NOT NULL,wiki_name TEXT NOT NULL,node_token TEXT NOT NULL,
            link TEXT NOT NULL,content TEXT NOT NULL,keywords_json TEXT NOT NULL,
            technical_entities_json TEXT NOT NULL,source TEXT NOT NULL,review_result TEXT NOT NULL,
            local_status TEXT NOT NULL,source_updated_at TEXT NOT NULL,indexed_at TEXT NOT NULL)"""
        )
        connection.execute(
            "INSERT INTO similarity_articles VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("legacy", "旧文章", "库", "node", "link", "旧规则摘要", '["旧主题"]', '[]', "blocks", "pass", "AI通过待确认", "", ""),
        )
    store = SimilarityIndexStore(path)
    assert store.info()["schema_version"] == "3"
    rows = store.query(exclude_document_id="current")
    assert len(rows) == 1
    assert rows[0].content == "旧规则摘要"
    assert rows[0].topics == ["旧主题"]
    assert rows[0].summary_source == "deterministic_legacy"


def test_v2_index_is_migrated_to_v3_with_empty_new_feature_lists(tmp_path: Path) -> None:
    path = tmp_path / "articles-v2.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE similarity_index_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO similarity_index_metadata VALUES('schema_version','2')")
        connection.execute(
            """CREATE TABLE similarity_articles(
            document_id TEXT PRIMARY KEY,title TEXT NOT NULL,wiki_name TEXT NOT NULL,node_token TEXT NOT NULL,
            link TEXT NOT NULL,content TEXT NOT NULL,keywords_json TEXT NOT NULL,
            technical_entities_json TEXT NOT NULL,source TEXT NOT NULL,review_result TEXT NOT NULL,
            local_status TEXT NOT NULL,source_updated_at TEXT NOT NULL,indexed_at TEXT NOT NULL,
            topics_json TEXT NOT NULL DEFAULT '[]',key_parameters_json TEXT NOT NULL DEFAULT '[]',
            summary_source TEXT NOT NULL DEFAULT 'deterministic_legacy',overview_model TEXT NOT NULL DEFAULT '',
            overview_prompt_version TEXT NOT NULL DEFAULT '',source_content_hash TEXT NOT NULL DEFAULT '')"""
        )
        connection.execute(
            "INSERT INTO similarity_articles VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "v2",
                "旧AI概述",
                "库",
                "node",
                "link",
                "无人机飞控旧概述",
                '["无人机"]',
                '[]',
                "blocks",
                "pass",
                "AI通过待确认",
                "",
                "",
                '["无人机"]',
                '[]',
                "ai_article_overview",
                "kimi-k3",
                "article-overview-v1",
                "hash",
            ),
        )
    store = SimilarityIndexStore(path)
    assert store.info()["schema_version"] == "3"
    row = store.query(exclude_document_id="current")[0]
    assert row.summary_source == "ai_article_overview"
    assert row.methods == []
    assert row.application_scenarios == []
    assert row.validation_methods == []


def test_unknown_future_index_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE similarity_index_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO similarity_index_metadata VALUES('schema_version','99')")
    with pytest.raises(SimilarityIndexError):
        SimilarityIndexStore(path).initialize()


def test_index_schema_stores_overview_not_historical_files(tmp_path: Path) -> None:
    store = SimilarityIndexStore(tmp_path / "articles.sqlite")
    store.initialize()
    with sqlite3.connect(store.path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(similarity_articles)")}
    assert {"content", "topics_json", "key_parameters_json", "methods_json", "application_scenarios_json", "validation_methods_json", "summary_source", "overview_model", "overview_prompt_version", "source_content_hash"} <= columns
    assert not {"full_text", "pdf_path", "png_path", "image_path"} & columns


def test_index_text_fields_do_not_contain_request_secrets(tmp_path: Path) -> None:
    store = SimilarityIndexStore(tmp_path / "articles.sqlite")
    _upsert(store, "safe")
    with sqlite3.connect(store.path) as connection:
        row = connection.execute("SELECT * FROM similarity_articles").fetchone()
    serialized = " ".join(str(value) for value in row)
    for forbidden in ("Authorization", "Bearer ", ";base64,", "test-secret-key"):
        assert forbidden not in serialized


def test_new_environment_names_and_legacy_aliases(tmp_path: Path, monkeypatch) -> None:
    from wiki_review_v2.config import Settings

    monkeypatch.setenv("SIMILARITY_THRESHOLD", "0.42")
    monkeypatch.setenv("SIMILARITY_TOP_K", "7")
    monkeypatch.setenv("SIMILARITY_OVERVIEW_MAX_CHARS", "1000")
    monkeypatch.setenv("SIMILARITY_QUERY_MAX_CHARS", "25000")
    monkeypatch.setenv("RETRIEVAL_OVERVIEW_MIN_CHARS", "80")
    monkeypatch.setenv("RETRIEVAL_OVERVIEW_MAX_CHARS", "1100")
    monkeypatch.setenv("RETRIEVAL_OVERVIEW_PROMPT_VERSION", "retrieval-test-v2")
    monkeypatch.setenv("RETRIEVAL_OVERVIEW_MODEL", "kimi-overview-test")
    monkeypatch.setenv("SIMILARITY_INDEX_PATH", "custom-index/articles.sqlite")
    configured = Settings.from_env(tmp_path)
    assert configured.similarity_threshold == 0.42
    assert configured.similarity_top_k == 7
    assert configured.similarity_overview_max_chars == 1000
    assert configured.similarity_summary_max_chars == 1000
    assert configured.similarity_query_max_chars == 25000
    assert configured.retrieval_overview_min_chars == 80
    assert configured.retrieval_overview_max_chars == 1100
    assert configured.retrieval_overview_prompt_version == "retrieval-test-v2"
    assert configured.retrieval_overview_model == "kimi-overview-test"
    assert configured.similarity_index_path == tmp_path / "custom-index" / "articles.sqlite"


def test_legacy_summary_environment_name_is_supported(tmp_path: Path, monkeypatch) -> None:
    from wiki_review_v2.config import Settings

    monkeypatch.delenv("SIMILARITY_OVERVIEW_MAX_CHARS", raising=False)
    monkeypatch.setenv("SIMILARITY_SUMMARY_MAX_CHARS", "900")
    assert Settings.from_env(tmp_path).similarity_overview_max_chars == 900


def test_similar_chinese_text_scores_higher_and_formula_is_exact() -> None:
    scorer = TextSimilarityScorer()
    current = _profile("current")
    current_overview = _overview()
    scored = scorer.score_all(
        current, current_overview, [_indexed("similar"), _indexed("unrelated", unrelated=True)]
    )
    similar = scored[0][1]
    unrelated = scored[1][1]
    assert similar.final_score > unrelated.final_score
    assert similar.text_tfidf > unrelated.text_tfidf
    expected = (
        0.60 * similar.overview_content_tfidf
        + 0.10 * similar.title_similarity
        + 0.10 * similar.topic_keyword_jaccard
        + 0.05 * similar.entity_jaccard
        + 0.05 * similar.parameter_jaccard
        + 0.05 * similar.methods_jaccard
        + 0.05 * similar.scenarios_validation_jaccard
    )
    assert similar.final_score == pytest.approx(expected, abs=1e-6)


def test_threshold_top_k_and_stable_sort(settings, tmp_path: Path) -> None:
    configured = settings.model_copy(update={"similarity_threshold": 0.0, "similarity_top_k": 2})
    store = SimilarityIndexStore(tmp_path / "articles.sqlite")
    for document_id in ("beta", "alpha", "gamma"):
        _upsert(store, document_id)
    selected, audit = LocalSimilarityRetriever(configured, store).retrieve(
        current_document_id="current", current_profile=_profile("current")
        , current_overview=_overview()
    )
    assert [item.document_id for item in selected] == ["alpha", "beta"]
    assert len(audit.scored_candidates) == 3
    assert all(item.entered_prompt == item.selected_for_prompt for item in audit.scored_candidates)

    strict = configured.model_copy(update={"similarity_threshold": 1.0})
    selected, audit = LocalSimilarityRetriever(strict, store).retrieve(
        current_document_id="current",
        current_profile=_profile("current", content="完全不相关的当前文章，仅讨论植物生长。"),
        current_overview=_overview(unrelated=True),
    )
    assert selected == []
    assert all(not item.above_threshold for item in audit.scored_candidates)
