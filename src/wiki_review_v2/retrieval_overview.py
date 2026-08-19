from __future__ import annotations

import json
import re

from .config import Settings
from .models import (
    RetrievalArticleOverview,
    RetrievalArticleOverviewAudit,
    RetrievalOverviewValidation,
    SimilarityProfile,
    SourceDocument,
)


RETRIEVAL_OVERVIEW_SYSTEM_PROMPT = """你是科研知识库的文章检索概述生成器。

你的任务不是审稿，也不是判断文章是否重复、抄袭、通过或需要修改。你只能根据当前提供的文章正文，生成一份稳定、客观、适合与历史文章进行相似性比较的结构化概述。

要求：
1. 只描述当前文章，不得引入未在当前正文出现的信息。
2. 不得生成审稿意见，不得使用“建议修改”“可以通过”“存在问题”等审稿话术。
3. 不得推测图片、附件或未提交内容。
4. 不得参考历史候选；本阶段不会向你提供历史候选。
5. content 应概括主题、目标、技术路线、关键模块、主要实体、重要参数、实现过程、测试方法和结论。
6. 不要只复述标题、目录或摘要，应覆盖正文中真正有区分度的内容。
7. 不要堆砌零散关键词，content 必须是连贯、可读的中文。
8. 正常长文的 content 建议为 500～1200 个中文字符；短文允许更短。
9. 所有列表字段必须来自当前正文，相同含义的项目应合并、去重。
10. 只输出符合 JSON Schema 的合法 JSON，不得输出 Markdown 围栏、解释或前后缀文字。"""

_REVIEW_MARKERS = (
    "建议修改",
    "可以通过",
    "审稿通过",
    "本轮审稿",
    "上一轮审稿",
    "审稿结论",
    "存在问题",
    "符合知识库",
)
_COMMON_GROUNDING_GRAMS = {
    "本文",
    "文章",
    "介绍",
    "记录",
    "说明",
    "内容",
    "当前",
    "进行",
    "通过",
    "使用",
    "包括",
    "主要",
    "相关",
    "方法",
    "结果",
}


class RetrievalOverviewPromptBuilder:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def build(
        self,
        *,
        source: SourceDocument,
        profile: SimilarityProfile,
    ) -> str:
        metadata = {
            "document_id": source.document_id,
            "title": source.title,
            "wiki_name": source.wiki_name,
            "updated_at": source.updated_at,
        }
        requirements = {
            "content": (
                "当前文章的连续可读概述；正常长文建议500～"
                f"{self.settings.retrieval_overview_max_chars}字，短文允许更短"
            ),
            "topics": "核心主题字符串数组",
            "technical_entities": "正文明确出现的芯片、设备、算法、协议、软件、平台或型号",
            "key_parameters": "正文明确出现的重要数值、单位、版本、指标或实验条件",
            "methods": "设计、实现、分析、实验或验证方法",
            "application_scenarios": "解决的问题和适用场景",
            "validation_methods": "测试、实验、仿真、数据或验收方法",
        }
        sections = [
            ("Task", "生成当前文章的检索概述。不要执行审稿，不要判断与其他文章的关系。"),
            ("DocumentMetadata", json.dumps(metadata, ensure_ascii=False, indent=2)),
            ("ContentSource", profile.source),
            ("StructuredContent", profile.query_text),
            ("OutputRequirements", json.dumps(requirements, ensure_ascii=False, indent=2)),
        ]
        return "\n\n".join(f"[{name}]\n{value}" for name, value in sections)


class RetrievalOverviewValidator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def normalize_and_validate(
        self,
        *,
        document_id: str,
        overview: RetrievalArticleOverview,
        profile: SimilarityProfile,
        model: str,
    ) -> tuple[RetrievalArticleOverview, RetrievalArticleOverviewAudit]:
        normalized = RetrievalArticleOverview(
            content=_clean_content(overview.content),
            topics=_unique(overview.topics, 20),
            technical_entities=_unique(overview.technical_entities, 40),
            key_parameters=_unique(overview.key_parameters, 40),
            methods=_unique(overview.methods, 30),
            application_scenarios=_unique(overview.application_scenarios, 20),
            validation_methods=_unique(overview.validation_methods, 30),
        )
        warnings: list[str] = []
        errors: list[str] = []
        content_key = _normalize(normalized.content)
        source_key = _normalize(profile.query_text)

        if not content_key:
            errors.append("retrieval_overview_content_empty")
        if len(normalized.content) > self.settings.retrieval_overview_max_chars:
            errors.append("retrieval_overview_exceeds_max_chars")
        if (
            len(normalized.content) < self.settings.retrieval_overview_min_chars
            and profile.source_character_count >= self.settings.retrieval_overview_min_chars * 3
        ):
            warnings.append("retrieval_overview_short_for_source")
        if any(marker in normalized.content for marker in _REVIEW_MARKERS):
            errors.append("retrieval_overview_contains_review_language")
        if content_key and source_key and not _has_grounding(content_key, source_key):
            errors.append("retrieval_overview_not_grounded_in_source")

        for item in normalized.technical_entities:
            if _normalize(item) not in source_key:
                warnings.append(f"ungrounded_technical_entity:{item}")
        for item in normalized.key_parameters:
            if _normalize(item) not in source_key:
                warnings.append(f"ungrounded_key_parameter:{item}")

        warnings = list(dict.fromkeys(warnings))
        errors = list(dict.fromkeys(errors))
        audit = RetrievalArticleOverviewAudit(
            document_id=document_id,
            content_source=profile.source,
            source_content_hash=profile.source_content_hash,
            model=model,
            prompt_version=self.settings.retrieval_overview_prompt_version,
            overview=normalized,
            validation=RetrievalOverviewValidation(
                valid=not errors,
                warnings=warnings,
                errors=errors,
            ),
        )
        return normalized, audit

    def unavailable_audit(
        self, *, document_id: str, profile: SimilarityProfile, reason: str
    ) -> RetrievalArticleOverviewAudit:
        return RetrievalArticleOverviewAudit(
            document_id=document_id,
            content_source=profile.source,
            source_content_hash=profile.source_content_hash,
            model="",
            prompt_version=self.settings.retrieval_overview_prompt_version,
            validation=RetrievalOverviewValidation(valid=False, errors=[reason]),
        )


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


def _has_grounding(content: str, source: str) -> bool:
    if len(content) < 2:
        return content in source
    grams = {
        content[index : index + 2]
        for index in range(len(content) - 1)
        if content[index : index + 2] not in _COMMON_GROUNDING_GRAMS
    }
    if not grams:
        return False
    overlap = sum(1 for gram in grams if gram in source)
    return overlap / len(grams) >= 0.02
