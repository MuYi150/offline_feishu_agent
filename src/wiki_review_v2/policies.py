from __future__ import annotations

from .models import PreviousReview


class ReviewModePolicy:                    #判断初复审
    def decide(self, review_round: int, previous_review: PreviousReview | None) -> str:
        return "rereview" if review_round > 0 or previous_review is not None else "initial"


class ReviewHistoryPolicy:#筛选需要重点复查的问题
    def blocking_context(self, review: PreviousReview | None) -> list[dict[str, object]]:
        if review is None:
            return []
        return [
            issue.model_dump(mode="json")
            for issue in review.issues
            if issue.level.value in {"blocking", "major"}
        ]
