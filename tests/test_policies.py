from __future__ import annotations

from wiki_review_v2.models import PreviousReview
from wiki_review_v2.policies import ReviewHistoryPolicy, ReviewModePolicy


def test_review_mode_initial_and_rereview() -> None:
    policy = ReviewModePolicy()
    assert policy.decide(0) == "initial"
    assert policy.decide(1) == "rereview"
    assert policy.decide(3) == "rereview"


def test_rereview_context_only_keeps_blocking_and_major() -> None:
    previous = PreviousReview.model_validate(
        {
            "document_id": "d",
            "review_round": 1,
            "result": "need_revision",
            "issues": [
                {
                    "issue_id": "a",
                    "level": "major",
                    "category": "x",
                    "position": "第一节",
                    "problem": "p",
                    "suggestion": "s",
                    "evidence_ids": ["page-1"],
                },
                {"issue_id": "b", "level": "minor", "category": "x", "problem": "p"},
            ],
        }
    )
    context = ReviewHistoryPolicy().blocking_context(previous)
    assert [item["issue_id"] for item in context] == ["a"]
    assert context[0]["position"] == "第一节"
    assert context[0]["evidence_ids"] == ["page-1"]
