from __future__ import annotations

from wiki_review_v2.models import PreviousReview
from wiki_review_v2.policies import ReviewHistoryPolicy, ReviewModePolicy


def test_review_mode_initial_and_rereview() -> None:
    policy = ReviewModePolicy()
    assert policy.decide(0, None) == "initial"
    assert policy.decide(1, None) == "rereview"


def test_rereview_context_only_keeps_blocking_and_major() -> None:
    previous = PreviousReview.model_validate(
        {
            "document_id": "d",
            "review_round": 1,
            "result": "need_revision",
            "issues": [
                {"issue_id": "a", "level": "major", "category": "x", "problem": "p"},
                {"issue_id": "b", "level": "minor", "category": "x", "problem": "p"},
            ],
        }
    )
    assert [item["issue_id"] for item in ReviewHistoryPolicy().blocking_context(previous)] == ["a"]
