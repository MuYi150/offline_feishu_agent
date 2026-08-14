from __future__ import annotations

import re
from typing import Any

from .config import Settings
from .models import ArticleOverview, ArticleOverviewAudit, SimilarityProfile


OVERVIEW_PROMPT_VERSION = "article-overview-v1"
_REVIEW_PROCESS_MARKERS = (
    "本轮审稿",
    "上一轮审稿",
    "审稿通过",
    "本次审稿结论",
    "已解决上一轮",
    "符合知识库公示标准",
)


class ArticleOverviewValidator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def normalize_and_validate(
        self,
        *,
        document_id: str,
        overview: ArticleOverview | None,
        review_summary: str,
        profile: SimilarityProfile,
        candidates: list[dict[str, Any]],
        overview_model: str,
    ) -> tuple[ArticleOverview | None, ArticleOverviewAudit]:
        if overview is None:
            audit = ArticleOverviewAudit(
                document_id=document_id,
                overview_model=overview_model,
                source_content_hash=profile.source_content_hash,
                validation_status="unavailable",
                validation_warnings=["article_overview_unavailable"],
            )
            return None, audit

        normalized = ArticleOverview(
            content=_clean_content(overview.content),
            topics=_unique(overview.topics, 20),
            technical_entities=_unique(overview.technical_entities, 40),
            key_parameters=_unique(overview.key_parameters, 40),
        )
        warnings: list[str] = []
        invalid: list[str] = []
        content_key = _normalize(normalized.content)
        source_key = _normalize(profile.query_text)

        if not content_key:
            invalid.append("article_overview_content_empty")
        if len(normalized.content) > self.settings.similarity_overview_max_chars:
            invalid.append("article_overview_exceeds_max_chars")
        if content_key and content_key == _normalize(review_summary):
            invalid.append("article_overview_equals_review_summary")
        if any(marker in normalized.content for marker in _REVIEW_PROCESS_MARKERS):
            invalid.append("article_overview_contains_review_process_language")
        if len(normalized.content) < 100 and profile.source_character_count >= 500:
            warnings.append("article_overview_short_for_source")

        for item in normalized.technical_entities:
            if _normalize(item) not in source_key:
                invalid.append(f"ungrounded_technical_entity:{item}")
        for item in normalized.key_parameters:
            if _normalize(item) not in source_key:
                invalid.append(f"ungrounded_key_parameter:{item}")

        for candidate in candidates:
            for field in ("document_id", "title"):
                marker = str(candidate.get(field, "")).strip()
                marker_key = _normalize(marker)
                if marker_key and marker_key in content_key and marker_key not in source_key:
                    invalid.append(f"candidate_{field}_contamination:{marker}")

        all_messages = list(dict.fromkeys([*invalid, *warnings]))
        status = "invalid" if invalid else ("warning" if warnings else "valid")
        audit = ArticleOverviewAudit(
            document_id=document_id,
            article_overview=normalized,
            overview_model=overview_model,
            overview_prompt_version=OVERVIEW_PROMPT_VERSION,
            source_content_hash=profile.source_content_hash,
            validation_status=status,
            validation_warnings=all_messages,
        )
        return normalized, audit


def _clean_content(value: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in str(value).splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _normalize(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value).lower())


def _unique(values: list[str], limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = re.sub(r"\s+", " ", str(value)).strip()
        key = _normalize(item)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= limit:
            break
    return result
