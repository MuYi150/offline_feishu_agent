from __future__ import annotations

from .models import PreviousReview, SimilarityCandidate


class ReviewModePolicy:
    def decide(self, review_round: int, previous_review: PreviousReview | None) -> str:
        return "rereview" if review_round > 1 or previous_review is not None else "initial"


class SimilarityService:
    EFFECTIVE_STATUSES = {"已公示", "待审核", "published", "under_review", "active"}

    def __init__(self, *, min_score: float = 0.35, top_n: int = 5) -> None:
        self.min_score = min_score
        self.top_n = top_n

    def retrieve(self, current_document_id: str, candidates: list[SimilarityCandidate]) -> list[SimilarityCandidate]:
        valid = [
            item
            for item in candidates
            if item.document_id != current_document_id
            and item.status in self.EFFECTIVE_STATUSES
            and bool(item.content.strip())
            and item.score >= self.min_score
        ]
        return sorted(valid, key=lambda item: (-item.score, item.document_id))[: self.top_n]


class ReviewHistoryPolicy:
    def blocking_context(self, review: PreviousReview | None) -> list[dict[str, object]]:
        if review is None:
            return []
        return [
            issue.model_dump(mode="json")
            for issue in review.issues
            if issue.level.value in {"blocking", "major"}
        ]

