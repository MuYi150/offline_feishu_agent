from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from .errors import ModelJsonError, ModelSchemaError
from .models import (
    InputCoverage,
    IssueLevel,
    ModelReviewPayload,
    ReviewOutcome,
    ReviewResult,
    SimilarityDecision,
    SuggestedNextAction,
)


KNOWN_CATEGORIES = {
    "format",
    "structure",
    "correctness",
    "reproducibility",
    "safety",
    "placement",
    "similarity",
    "visibility",
    "learning_trace",
    "content_sufficiency",
    "content_placeholder",
    "visual_content_missing",
    "sensitive_content_review",
    "ai_generation_artifact",
    "ai_generated_without_personal_trace",
    "ai_revision_padding",
    "fabricated_content",
    "other",
}
DIRECT_REJECT_CATEGORIES = {
    "ai_generation_artifact",
    "ai_generated_without_personal_trace",
    "ai_revision_padding",
    "fabricated_content",
}
SIMILARITY_DECISIONS = {
    "no_similar": SimilarityDecision.KEEP_INDEPENDENT,
    "same_area_different_direction": SimilarityDecision.KEEP_INDEPENDENT,
    "related_but_keep": SimilarityDecision.KEEP_INDEPENDENT,
    "not_applicable": SimilarityDecision.KEEP_INDEPENDENT,
    "merge_recommended": SimilarityDecision.MERGE_REQUIRED,
    "duplicate_reject_recommended": SimilarityDecision.REJECT_INDEPENDENT_SUBMISSION,
}


class ReviewResultParser:
    def parse(self, raw: str) -> ModelReviewPayload:
        text = str(raw or "").strip()
        if not text:
            raise ModelJsonError("模型返回空内容")
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ModelJsonError("模型返回内容不是合法 JSON") from exc
        try:
            return ModelReviewPayload.model_validate(payload)
        except ValidationError as exc:
            raise ModelSchemaError("模型 JSON 不符合审稿 Schema") from exc


class ReviewResultNormalizer:
    def normalize(
        self,
        payload: ModelReviewPayload,
        coverage: InputCoverage,
        *,
        review_mode: str = "initial",
        effective_candidate_count: int | None = None,
    ) -> ReviewResult:
        data = payload.model_dump(mode="json")
        issues = data["issues"]
        for issue in issues:
            if issue["category"] not in KNOWN_CATEGORIES:
                issue["category"] = "other"
            if issue["category"] == "format" and "标题" in issue["position"]:
                issue["level"] = IssueLevel.MINOR.value
            if issue["category"] == "content_placeholder" and "【表格】" in issue["problem"]:
                issue["level"] = IssueLevel.MINOR.value
                issue["category"] = "format"

        counts = {
            level: sum(1 for issue in issues if issue["level"] == level)
            for level in ("blocking", "major", "minor")
        }
        data["blocking_count"] = counts["blocking"]
        data["major_count"] = counts["major"]
        data["minor_count"] = counts["minor"]

        similarity = data["similarity_check"]
        if review_mode == "rereview":
            similarity.update(
                status="not_applicable",
                summary="复审不重新召回相似候选。",
                candidates_considered=[],
            )
        elif effective_candidate_count == 0:
            similarity.update(
                status="no_similar",
                summary="未发现明显相似文章。",
                candidates_considered=[],
            )
        similarity["decision"] = SIMILARITY_DECISIONS[similarity["status"]].value
        similarity["candidate_count"] = len(similarity["candidates_considered"])

        assessment = data["visual_evidence_assessment"]
        if not coverage.visual_pages_complete:
            assessment["coverage"] = "partial" if assessment["pages_reviewed"] else "unavailable"
            assessment["limitations"] = list(dict.fromkeys([*assessment["limitations"], *coverage.limitations]))

        direct_reject = any(
            issue["level"] == "blocking" and issue["category"] in DIRECT_REJECT_CATEGORIES for issue in issues
        )
        requested = ReviewOutcome(data["result"])
        coverage_incomplete = (
            not coverage.visual_pages_complete or coverage.input_truncated or bool(coverage.missing_sources)
        )

        if coverage_incomplete and requested in {ReviewOutcome.PASS, ReviewOutcome.REJECT}:
            outcome = ReviewOutcome.INCOMPLETE_REVIEW
        elif direct_reject:
            outcome = ReviewOutcome.REJECT
        elif counts["blocking"] or counts["major"]:
            outcome = ReviewOutcome.NEED_REVISION
        elif requested in {ReviewOutcome.RECOMMEND_HUMAN_REVIEW, ReviewOutcome.INCOMPLETE_REVIEW}:
            outcome = requested
        else:
            outcome = ReviewOutcome.PASS

        if similarity["decision"] in {
            SimilarityDecision.MERGE_REQUIRED.value,
            SimilarityDecision.REJECT_INDEPENDENT_SUBMISSION.value,
        } and outcome == ReviewOutcome.PASS:
            outcome = ReviewOutcome.NEED_REVISION
            issues.append(
                {
                    "issue_id": "similarity-decision",
                    "level": "major",
                    "category": "similarity",
                    "position": "全文",
                    "problem": "相似性结论要求合并或不建议独立提交。",
                    "suggestion": "依据相似性比较结论调整投稿方式。",
                    "evidence_ids": [],
                }
            )
            data["major_count"] += 1

        data["result"] = outcome.value
        data["suggested_next_action"] = self._next_action(outcome, SimilarityDecision(similarity["decision"])).value
        if outcome != ReviewOutcome.PASS:
            data["pass_reason"] = ""
        return ReviewResult(**data, input_coverage=coverage)

    @staticmethod
    def _next_action(outcome: ReviewOutcome, similarity: SimilarityDecision) -> SuggestedNextAction:
        if similarity == SimilarityDecision.MERGE_REQUIRED:
            return SuggestedNextAction.MERGE_WITH_EXISTING
        if similarity == SimilarityDecision.REJECT_INDEPENDENT_SUBMISSION:
            return SuggestedNextAction.REJECT_INDEPENDENT_SUBMISSION
        return {
            ReviewOutcome.PASS: SuggestedNextAction.ADMIN_CONFIRM,
            ReviewOutcome.NEED_REVISION: SuggestedNextAction.AUTHOR_REVISE,
            ReviewOutcome.RECOMMEND_HUMAN_REVIEW: SuggestedNextAction.HUMAN_REVIEW,
            ReviewOutcome.INCOMPLETE_REVIEW: SuggestedNextAction.HUMAN_RECHECK,
            ReviewOutcome.REJECT: SuggestedNextAction.REJECT,
        }[outcome]


class ReviewResultValidator:
    def validate(
        self,
        result: ReviewResult,
        *,
        required_rereview_issue_ids: list[str] | None = None,
    ) -> None:
        counts = {
            level: sum(1 for issue in result.issues if issue.level.value == level)
            for level in ("blocking", "major", "minor")
        }
        if (result.blocking_count, result.major_count, result.minor_count) != (
            counts["blocking"], counts["major"], counts["minor"]
        ):
            raise ModelSchemaError("问题计数与 issues 不一致")
        if result.result == ReviewOutcome.NEED_REVISION and not (counts["blocking"] or counts["major"]):
            raise ModelSchemaError("need_revision 必须包含 blocking 或 major")
        if result.result == ReviewOutcome.PASS and (counts["blocking"] or counts["major"]):
            raise ModelSchemaError("pass 不得包含 blocking 或 major")
        if result.result == ReviewOutcome.REJECT and not any(
            issue.level == IssueLevel.BLOCKING and issue.category in DIRECT_REJECT_CATEGORIES
            for issue in result.issues
        ):
            raise ModelSchemaError("reject 必须包含直接拒稿类 blocking")
        if (
            not result.input_coverage.visual_pages_complete
            or result.input_coverage.input_truncated
            or result.input_coverage.missing_sources
        ) and result.result in {
            ReviewOutcome.PASS,
            ReviewOutcome.REJECT,
        }:
            raise ModelSchemaError("输入覆盖不足时不得 pass 或 reject")
        expected = SIMILARITY_DECISIONS[result.similarity_check.status]
        if result.similarity_check.decision != expected:
            raise ModelSchemaError("相似性状态与 decision 不一致")
        if result.similarity_check.decision != SimilarityDecision.KEEP_INDEPENDENT and not result.similarity_check.candidates_considered:
            raise ModelSchemaError("合并或不建议独立提交必须引用至少一个有效相似候选")
        if required_rereview_issue_ids is not None:
            actual = [item.issue_id for item in result.re_review_assessment.resolutions]
            if len(actual) != len(set(actual)):
                raise ModelSchemaError("复审 resolutions 不得包含重复 issue_id")
            if set(actual) != set(required_rereview_issue_ids):
                raise ModelSchemaError("复审 resolutions 必须完整且仅覆盖上一轮 blocking/major issue_id")


class ReviewOutcomeMapper:
    STATUS = {
        ReviewOutcome.PASS: "AI通过待确认",
        ReviewOutcome.NEED_REVISION: "需修改",
        ReviewOutcome.RECOMMEND_HUMAN_REVIEW: "待分配人工审稿",
        ReviewOutcome.INCOMPLETE_REVIEW: "待分配人工审稿",
        ReviewOutcome.REJECT: "已拒稿",
    }

    def map(self, outcome: ReviewOutcome) -> str:
        return self.STATUS[outcome]


def incomplete_payload(reason: str) -> ModelReviewPayload:
    return ModelReviewPayload.model_validate(
        {
            "result": "incomplete_review",
            "summary": f"自动审稿输入不完整：{reason}",
            "pass_reason": "",
            "blocking_count": 0,
            "major_count": 0,
            "minor_count": 0,
            "issues": [],
            "similarity_check": {
                "status": "not_applicable",
                "decision": "keep_independent",
                "summary": "输入不完整，未作相似性结论。",
                "candidate_count": 0,
                "candidates_considered": [],
            },
            "learning_trace_assessment": "输入不完整，无法完成判断。",
            "visual_evidence_assessment": {
                "coverage": "unavailable",
                "pages_reviewed": [],
                "evidence_used": [],
                "limitations": [reason],
            },
            "revision_priority": [],
            "suggested_next_action": "human_recheck",
            "re_review_assessment": {"resolutions": []},
            "article_overview": None,
        }
    )
