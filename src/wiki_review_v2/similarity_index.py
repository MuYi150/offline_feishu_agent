from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path

from .config import Settings
from .errors import SimilarityIndexError
from .models import (
    ArticleOverview,
    RetrievalArticleOverview,
    ReviewOutcome,
    SimilarityProfile,
    SimilarityPromptCandidate,
    SimilarityRetrievalAudit,
    SimilarityScoreDetails,
    SimilarityScoredCandidate,
    SourceDocument,
)


SCHEMA_VERSION = "3"


@dataclass(frozen=True)
class IndexedSimilarityArticle:
    document_id: str
    title: str
    wiki_name: str
    node_token: str
    link: str
    content: str
    topics: list[str]
    technical_entities: list[str]
    key_parameters: list[str]
    summary_source: str
    overview_model: str
    overview_prompt_version: str
    source_content_hash: str
    source: str
    review_result: str
    local_status: str
    source_updated_at: str
    indexed_at: str
    methods: list[str] = field(default_factory=list)
    application_scenarios: list[str] = field(default_factory=list)
    validation_methods: list[str] = field(default_factory=list)

    @property
    def keywords(self) -> list[str]:
        return self.topics


class SimilarityIndexStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        try:
            with self._connection() as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS similarity_index_metadata "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                existing = connection.execute(
                    "SELECT value FROM similarity_index_metadata WHERE key='schema_version'"
                ).fetchone()
                table_exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='similarity_articles'"
                ).fetchone()
                version = str(existing["value"]) if existing else None
                if version not in {None, "1", "2", SCHEMA_VERSION}:
                    raise SimilarityIndexError(
                        f"相似性索引 Schema 版本不兼容：{version}，当前支持 1/2→{SCHEMA_VERSION}"
                    )
                if not table_exists:
                    self._create_v3_table(connection)
                else:
                    self._migrate_to_v3(connection)
                connection.execute(
                    "INSERT INTO similarity_index_metadata(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (SCHEMA_VERSION,),
                )
        except SimilarityIndexError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise SimilarityIndexError("无法初始化或迁移本地相似性索引") from exc

    @staticmethod
    def _create_v3_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE similarity_articles (
                document_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                wiki_name TEXT NOT NULL,
                node_token TEXT NOT NULL,
                link TEXT NOT NULL,
                content TEXT NOT NULL,
                keywords_json TEXT NOT NULL,
                technical_entities_json TEXT NOT NULL,
                source TEXT NOT NULL,
                review_result TEXT NOT NULL,
                local_status TEXT NOT NULL,
                source_updated_at TEXT NOT NULL,
                indexed_at TEXT NOT NULL,
                topics_json TEXT NOT NULL DEFAULT '[]',
                key_parameters_json TEXT NOT NULL DEFAULT '[]',
                summary_source TEXT NOT NULL DEFAULT 'deterministic_legacy',
                overview_model TEXT NOT NULL DEFAULT '',
                overview_prompt_version TEXT NOT NULL DEFAULT '',
                source_content_hash TEXT NOT NULL DEFAULT '',
                methods_json TEXT NOT NULL DEFAULT '[]',
                application_scenarios_json TEXT NOT NULL DEFAULT '[]',
                validation_methods_json TEXT NOT NULL DEFAULT '[]'
            )
            """
        )

    @staticmethod
    def _migrate_to_v3(connection: sqlite3.Connection) -> None:
        existing_columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(similarity_articles)")
        }
        additions = {
            "topics_json": "TEXT NOT NULL DEFAULT '[]'",
            "key_parameters_json": "TEXT NOT NULL DEFAULT '[]'",
            "summary_source": "TEXT NOT NULL DEFAULT 'deterministic_legacy'",
            "overview_model": "TEXT NOT NULL DEFAULT ''",
            "overview_prompt_version": "TEXT NOT NULL DEFAULT ''",
            "source_content_hash": "TEXT NOT NULL DEFAULT ''",
            "methods_json": "TEXT NOT NULL DEFAULT '[]'",
            "application_scenarios_json": "TEXT NOT NULL DEFAULT '[]'",
            "validation_methods_json": "TEXT NOT NULL DEFAULT '[]'",
        }
        for name, declaration in additions.items():
            if name not in existing_columns:
                connection.execute(f"ALTER TABLE similarity_articles ADD COLUMN {name} {declaration}")
        connection.execute(
            "UPDATE similarity_articles SET topics_json=keywords_json "
            "WHERE topics_json='[]' OR topics_json=''"
        )
        connection.execute(
            "UPDATE similarity_articles SET summary_source='deterministic_legacy' "
            "WHERE summary_source=''"
        )

    def info(self) -> dict[str, object]:
        self.initialize()
        try:
            with self._connection() as connection:
                version = connection.execute(
                    "SELECT value FROM similarity_index_metadata WHERE key='schema_version'"
                ).fetchone()["value"]
                count = connection.execute(
                    "SELECT COUNT(*) AS count FROM similarity_articles"
                ).fetchone()["count"]
                sources = {
                    row["summary_source"]: row["count"]
                    for row in connection.execute(
                        "SELECT summary_source, COUNT(*) AS count FROM similarity_articles "
                        "GROUP BY summary_source ORDER BY summary_source"
                    )
                }
            return {
                "path": str(self.path.resolve()),
                "schema_version": version,
                "record_count": count,
                "summary_sources": sources,
            }
        except (OSError, sqlite3.Error) as exc:
            raise SimilarityIndexError("无法读取本地相似性索引信息") from exc

    def upsert(
        self,
        *,
        source: SourceDocument,
        profile: SimilarityProfile,
        article_overview: RetrievalArticleOverview | ArticleOverview,
        overview_model: str,
        overview_prompt_version: str = "retrieval_overview_v1",
        source_content_hash: str,
        review_result: ReviewOutcome | str,
        local_status: str,
    ) -> None:
        result = review_result.value if isinstance(review_result, ReviewOutcome) else str(review_result)
        if result != ReviewOutcome.PASS.value:
            raise SimilarityIndexError("只有审稿通过的文章可以写入相似性索引")
        if profile.source not in {"blocks", "pdf"} or not article_overview.content.strip():
            raise SimilarityIndexError("只有包含可靠 AI 文章概述的文章可以写入相似性索引")
        if source_content_hash != profile.source_content_hash or not source_content_hash:
            raise SimilarityIndexError("文章概述与当前正文内容哈希不一致")
        self.initialize()
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO similarity_articles(
                        document_id, title, wiki_name, node_token, link, content,
                        keywords_json, technical_entities_json, source, review_result,
                        local_status, source_updated_at, indexed_at, topics_json,
                        key_parameters_json, summary_source, overview_model,
                        overview_prompt_version, source_content_hash, methods_json,
                        application_scenarios_json, validation_methods_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(document_id) DO UPDATE SET
                        title=excluded.title,
                        wiki_name=excluded.wiki_name,
                        node_token=excluded.node_token,
                        link=excluded.link,
                        content=excluded.content,
                        keywords_json=excluded.keywords_json,
                        technical_entities_json=excluded.technical_entities_json,
                        source=excluded.source,
                        review_result=excluded.review_result,
                        local_status=excluded.local_status,
                        source_updated_at=excluded.source_updated_at,
                        indexed_at=excluded.indexed_at,
                        topics_json=excluded.topics_json,
                        key_parameters_json=excluded.key_parameters_json,
                        summary_source=excluded.summary_source,
                        overview_model=excluded.overview_model,
                        overview_prompt_version=excluded.overview_prompt_version,
                        source_content_hash=excluded.source_content_hash,
                        methods_json=excluded.methods_json,
                        application_scenarios_json=excluded.application_scenarios_json,
                        validation_methods_json=excluded.validation_methods_json
                    """,
                    (
                        source.document_id,
                        source.title,
                        source.wiki_name,
                        source.node_token,
                        source.link,
                        article_overview.content,
                        json.dumps(article_overview.topics, ensure_ascii=False),
                        json.dumps(article_overview.technical_entities, ensure_ascii=False),
                        profile.source,
                        result,
                        local_status,
                        source.updated_at,
                        datetime.now(UTC).isoformat(),
                        json.dumps(article_overview.topics, ensure_ascii=False),
                        json.dumps(article_overview.key_parameters, ensure_ascii=False),
                        "ai_retrieval_overview",
                        overview_model,
                        overview_prompt_version,
                        source_content_hash,
                        json.dumps(getattr(article_overview, "methods", []), ensure_ascii=False),
                        json.dumps(
                            getattr(article_overview, "application_scenarios", []),
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            getattr(article_overview, "validation_methods", []),
                            ensure_ascii=False,
                        ),
                    ),
                )
        except (OSError, sqlite3.Error) as exc:
            raise SimilarityIndexError("无法写入本地相似性索引") from exc

    def query(self, *, exclude_document_id: str) -> list[IndexedSimilarityArticle]:
        self.initialize()
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM similarity_articles WHERE document_id <> ? "
                    "AND review_result = 'pass' ORDER BY document_id",
                    (exclude_document_id,),
                ).fetchall()
            return [
                IndexedSimilarityArticle(
                    document_id=row["document_id"],
                    title=row["title"],
                    wiki_name=row["wiki_name"],
                    node_token=row["node_token"],
                    link=row["link"],
                    content=row["content"],
                    topics=list(json.loads(row["topics_json"])),
                    technical_entities=list(json.loads(row["technical_entities_json"])),
                    key_parameters=list(json.loads(row["key_parameters_json"])),
                    methods=list(json.loads(row["methods_json"])),
                    application_scenarios=list(
                        json.loads(row["application_scenarios_json"])
                    ),
                    validation_methods=list(json.loads(row["validation_methods_json"])),
                    summary_source=row["summary_source"],
                    overview_model=row["overview_model"],
                    overview_prompt_version=row["overview_prompt_version"],
                    source_content_hash=row["source_content_hash"],
                    source=row["source"],
                    review_result=row["review_result"],
                    local_status=row["local_status"],
                    source_updated_at=row["source_updated_at"],
                    indexed_at=row["indexed_at"],
                )
                for row in rows
            ]
        except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SimilarityIndexError("无法查询本地相似性索引") from exc


class TextSimilarityScorer:
    TEXT_WEIGHT = 0.60
    TITLE_WEIGHT = 0.10
    TOPIC_WEIGHT = 0.10
    ENTITY_WEIGHT = 0.05
    PARAMETER_WEIGHT = 0.05
    METHODS_WEIGHT = 0.05
    SCENARIOS_VALIDATION_WEIGHT = 0.05

    def score_all(
        self,
        current: SimilarityProfile,
        current_overview: RetrievalArticleOverview,
        candidates: list[IndexedSimilarityArticle],
    ) -> list[tuple[IndexedSimilarityArticle, SimilarityScoreDetails]]:
        text_scores = self._tfidf_scores(
            current_overview.content, [item.content for item in candidates]
        )
        scored: list[tuple[IndexedSimilarityArticle, SimilarityScoreDetails]] = []
        for candidate, text_score in zip(candidates, text_scores, strict=True):
            title_score = SequenceMatcher(
                None, _normalize(current.title), _normalize(candidate.title)
            ).ratio()
            topic_score = _jaccard(current_overview.topics, candidate.topics)
            entity_score = _jaccard(
                current_overview.technical_entities, candidate.technical_entities
            )
            parameter_score = _jaccard(
                current_overview.key_parameters, candidate.key_parameters
            )
            methods_score = _jaccard(current_overview.methods, candidate.methods)
            scenarios_validation_score = _jaccard(
                [*current_overview.application_scenarios, *current_overview.validation_methods],
                [*candidate.application_scenarios, *candidate.validation_methods],
            )
            final = (
                self.TEXT_WEIGHT * text_score
                + self.TITLE_WEIGHT * title_score
                + self.TOPIC_WEIGHT * topic_score
                + self.ENTITY_WEIGHT * entity_score
                + self.PARAMETER_WEIGHT * parameter_score
                + self.METHODS_WEIGHT * methods_score
                + self.SCENARIOS_VALIDATION_WEIGHT * scenarios_validation_score
            )
            scored.append(
                (
                    candidate,
                    SimilarityScoreDetails(
                        text_tfidf=_score(text_score),
                        overview_content_tfidf=_score(text_score),
                        title_similarity=_score(title_score),
                        topic_keyword_jaccard=_score(topic_score),
                        entity_jaccard=_score(entity_score),
                        parameter_jaccard=_score(parameter_score),
                        methods_jaccard=_score(methods_score),
                        scenarios_validation_jaccard=_score(scenarios_validation_score),
                        final_score=_score(final),
                    ),
                )
            )
        return scored

    @staticmethod
    def _tfidf_scores(current: str, candidates: list[str]) -> list[float]:
        documents = [_character_ngrams(current), *(_character_ngrams(item) for item in candidates)]
        if not candidates or not documents[0]:
            return [0.0] * len(candidates)
        document_count = len(documents)
        document_frequency: Counter[str] = Counter()
        for grams in documents:
            document_frequency.update(set(grams))
        vectors: list[dict[str, float]] = []
        for grams in documents:
            total = sum(grams.values()) or 1
            vectors.append(
                {
                    token: count / total
                    * (math.log((1 + document_count) / (1 + document_frequency[token])) + 1)
                    for token, count in grams.items()
                }
            )
        return [_cosine(vectors[0], item) for item in vectors[1:]]


class LocalSimilarityRetriever:
    def __init__(self, settings: Settings, store: SimilarityIndexStore) -> None:
        self.settings = settings
        self.store = store
        self.scorer = TextSimilarityScorer()

    def retrieve(
        self,
        *,
        current_document_id: str,
        current_profile: SimilarityProfile,
        current_overview: RetrievalArticleOverview,
    ) -> tuple[list[SimilarityPromptCandidate], SimilarityRetrievalAudit]:
        indexed = self.store.query(exclude_document_id=current_document_id)
        scored = (
            self.scorer.score_all(current_profile, current_overview, indexed)
            if current_overview.content
            else []
        )
        scored.sort(key=lambda item: (-item[1].final_score, item[0].document_id))
        above = [item for item in scored if item[1].final_score >= self.settings.similarity_threshold]
        selected = above[: self.settings.similarity_top_k]
        selected_ids = {item[0].document_id for item in selected}
        prompt_candidates = [
            SimilarityPromptCandidate(
                document_id=article.document_id,
                title=article.title,
                wiki_name=article.wiki_name,
                link=article.link,
                status=article.local_status,
                similarity_score=details.final_score,
                score_details=details,
                content=article.content,
                summary_source=article.summary_source,
                overview_model=article.overview_model,
                overview_prompt_version=article.overview_prompt_version,
            )
            for article, details in selected
        ]
        audit = SimilarityRetrievalAudit(
            query_document_id=current_document_id,
            index_candidate_count=len(indexed),
            threshold=self.settings.similarity_threshold,
            top_k=self.settings.similarity_top_k,
            scored_candidates=[
                SimilarityScoredCandidate(
                    document_id=article.document_id,
                    title=article.title,
                    wiki_name=article.wiki_name,
                    link=article.link,
                    status=article.local_status,
                    score_details=details,
                    above_threshold=details.final_score >= self.settings.similarity_threshold,
                    selected_for_prompt=article.document_id in selected_ids,
                    entered_prompt=article.document_id in selected_ids,
                    content=article.content,
                    summary_source=article.summary_source,
                    overview_model=article.overview_model,
                    overview_prompt_version=article.overview_prompt_version,
                )
                for article, details in scored
            ],
            prompt_candidates=prompt_candidates,
        )
        return prompt_candidates, audit


def _normalize(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.lower())


def _character_ngrams(value: str) -> Counter[str]:
    normalized = _normalize(value)
    grams: Counter[str] = Counter()
    for size in range(2, 5):
        grams.update(
            normalized[index : index + size]
            for index in range(max(0, len(normalized) - size + 1))
        )
    return grams


def _jaccard(left: list[str], right: list[str]) -> float:
    first = {_normalize(item) for item in left if _normalize(item)}
    second = {_normalize(item) for item in right if _normalize(item)}
    return len(first & second) / len(first | second) if first and second else 0.0


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(value * right.get(token, 0.0) for token, value in left.items())
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _score(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 6)
