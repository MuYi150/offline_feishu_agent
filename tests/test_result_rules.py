from __future__ import annotations

import json

import pytest

from wiki_review_v2.errors import ModelJsonError, ModelSchemaError
from wiki_review_v2.models import InputCoverage, ModelReviewPayload, ReviewOutcome
from wiki_review_v2.result import (
    ReviewOutcomeMapper,
    ReviewResultNormalizer,
    ReviewResultParser,
    ReviewResultValidator,
)


def payload(*, result: str = "pass", issues: list[dict[str, object]] | None = None) -> dict[str, object]:
    issues = issues or []
    return {
        "result": result,
        "summary": "summary",
        "pass_reason": "reason",
        "blocking_count": 99,
        "major_count": 99,
        "minor_count": 99,
        "issues": issues,
        "similarity_check": {
            "status": "no_similar",
            "decision": "keep_independent",
            "summary": "none",
            "candidate_count": 0,
            "candidates_considered": [],
        },
        "learning_trace_assessment": "ok",
        "visual_evidence_assessment": {
            "coverage": "complete",
            "pages_reviewed": [1],
            "evidence_used": [],
            "limitations": [],
        },
        "revision_priority": [],
        "suggested_next_action": "admin_confirm",
        "re_review_assessment": {"resolutions": []},
        "article_overview": {
            "content": "本文记录当前文章的主题、方法和验证结果。",
            "topics": ["技术记录"],
            "technical_entities": [],
            "key_parameters": [],
        },
    }


def coverage(*, complete: bool = True) -> InputCoverage:
    return InputCoverage(structured_text_available=True, visual_pages_complete=complete)


def test_parser_accepts_json_fence_and_rejects_non_json() -> None:
    parser = ReviewResultParser()
    assert parser.parse("```json\n" + json.dumps(payload()) + "\n```").result == ReviewOutcome.PASS
    with pytest.raises(ModelJsonError):
        parser.parse("not json")


def test_parser_rejects_missing_fields() -> None:
    with pytest.raises(ModelSchemaError):
        ReviewResultParser().parse('{"result":"pass"}')


def test_normalizer_recomputes_counts_and_downgrades_title_issue() -> None:
    item = {
        "issue_id": "i",
        "level": "major",
        "category": "format",
        "position": "标题",
        "problem": "标题空格",
        "suggestion": "调整",
        "evidence_ids": [],
    }
    result = ReviewResultNormalizer().normalize(ModelReviewPayload.model_validate(payload(issues=[item])), coverage())
    assert result.result == ReviewOutcome.PASS
    assert (result.blocking_count, result.major_count, result.minor_count) == (0, 0, 1)
    ReviewResultValidator().validate(result)


def test_table_marker_is_not_major_placeholder() -> None:
    item = {
        "issue_id": "i",
        "level": "major",
        "category": "content_placeholder",
        "position": "正文",
        "problem": "发现【表格】",
        "suggestion": "无",
        "evidence_ids": [],
    }
    result = ReviewResultNormalizer().normalize(ModelReviewPayload.model_validate(payload(issues=[item])), coverage())
    assert result.result == ReviewOutcome.PASS
    assert result.issues[0].category == "format"


def test_only_direct_reject_category_can_reject() -> None:
    generic = {
        "issue_id": "i",
        "level": "blocking",
        "category": "correctness",
        "position": "全文",
        "problem": "wrong",
        "suggestion": "fix",
        "evidence_ids": [],
    }
    direct = {**generic, "category": "fabricated_content"}
    normalizer = ReviewResultNormalizer()
    assert normalizer.normalize(ModelReviewPayload.model_validate(payload(result="reject", issues=[generic])), coverage()).result == ReviewOutcome.NEED_REVISION
    assert normalizer.normalize(ModelReviewPayload.model_validate(payload(result="reject", issues=[direct])), coverage()).result == ReviewOutcome.REJECT


def test_partial_coverage_forces_incomplete() -> None:
    result = ReviewResultNormalizer().normalize(ModelReviewPayload.model_validate(payload()), coverage(complete=False))
    assert result.result == ReviewOutcome.INCOMPLETE_REVIEW
    ReviewResultValidator().validate(result)


def test_outcome_mapping() -> None:
    mapper = ReviewOutcomeMapper()
    assert mapper.map(ReviewOutcome.PASS) == "AI通过待确认"
    assert mapper.map(ReviewOutcome.NEED_REVISION) == "需修改"
    assert mapper.map(ReviewOutcome.RECOMMEND_HUMAN_REVIEW) == "待分配人工审稿"
    assert mapper.map(ReviewOutcome.INCOMPLETE_REVIEW) == "待分配人工审稿"
    assert mapper.map(ReviewOutcome.REJECT) == "已拒稿"


@pytest.mark.parametrize(
    "resolution_ids",
    [["a"], ["a", "a"], ["a", "b", "extra"]],
)
def test_rereview_resolutions_must_exactly_cover_previous_issues(resolution_ids) -> None:
    raw = payload()
    raw["similarity_check"]["status"] = "not_applicable"
    raw["re_review_assessment"] = {
        "resolutions": [
            {"issue_id": issue_id, "status": "resolved", "evidence": "已补充"}
            for issue_id in resolution_ids
        ]
    }
    result = ReviewResultNormalizer().normalize(
        ModelReviewPayload.model_validate(raw), coverage(), review_mode="rereview"
    )
    with pytest.raises(ModelSchemaError):
        ReviewResultValidator().validate(
            result, required_rereview_issue_ids=["a", "b"]
        )


def test_rereview_resolutions_accept_exact_issue_set() -> None:
    raw = payload()
    raw["similarity_check"]["status"] = "not_applicable"
    raw["re_review_assessment"] = {
        "resolutions": [
            {"issue_id": issue_id, "status": "resolved", "evidence": "已补充"}
            for issue_id in ["b", "a"]
        ]
    }
    result = ReviewResultNormalizer().normalize(
        ModelReviewPayload.model_validate(raw), coverage(), review_mode="rereview"
    )
    ReviewResultValidator().validate(
        result, required_rereview_issue_ids=["a", "b"]
    )
