from __future__ import annotations

from wiki_review_v2.models import PreviousReview, SimilarityCandidate
from wiki_review_v2.policies import ReviewHistoryPolicy, ReviewModePolicy, SimilarityService


def test_review_mode_initial_and_rereview() -> None:
    policy = ReviewModePolicy()
    assert policy.decide(1, None) == "initial"
    assert policy.decide(2, None) == "rereview"


def test_similarity_filters_self_invalid_empty_and_low_score() -> None:
    candidates = [
        SimilarityCandidate(document_id="current", title="self", score=0.99, content="x"),
        SimilarityCandidate(document_id="bad", title="bad", score=0.98, content="x", status="已拒稿"),
        SimilarityCandidate(document_id="empty", title="empty", score=0.97, content=""),
        SimilarityCandidate(document_id="low", title="low", score=0.34, content="x"),
        SimilarityCandidate(document_id="b", title="b", score=0.8, content="x"),
        SimilarityCandidate(document_id="a", title="a", score=0.8, content="x"),
    ]
    result = SimilarityService(min_score=0.35, top_n=2).retrieve("current", candidates)
    assert [item.document_id for item in result] == ["a", "b"]


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

