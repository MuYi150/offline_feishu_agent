from __future__ import annotations

from .models import ReviewOutcome, ReviewResult, SourceDocument


class NotificationRenderer:
    def render(self, source: SourceDocument, result: ReviewResult) -> tuple[str, str]:
        heading = f"《{source.title}》第 {source.review_round} 轮审稿"
        issue_lines = [
            f"{index}. [{issue.level.value}/{issue.category}] {issue.problem}\n   建议：{issue.suggestion}"
            for index, issue in enumerate(result.issues, 1)
            if issue.level.value in {"blocking", "major"}
        ]
        important = "\n".join(issue_lines) if issue_lines else "无 blocking/major 问题。"
        author_action = {
            ReviewOutcome.PASS: "已通过 AI 初筛，等待管理员确认。",
            ReviewOutcome.NEED_REVISION: "请根据以下关键问题修改后重新提交。",
            ReviewOutcome.RECOMMEND_HUMAN_REVIEW: "AI 已完成基础初筛，后续需要人工判断。",
            ReviewOutcome.INCOMPLETE_REVIEW: "自动审稿输入不完整，后续需要人工复核。",
            ReviewOutcome.REJECT: "发现直接拒稿类问题，本次投稿未通过。",
        }[result.result]
        submitter = f"一、审稿结论\n{heading}\n{author_action}\n\n二、摘要\n{result.summary}\n\n三、关键问题\n{important}\n"
        admin = (
            f"一、管理员审稿摘要\n{heading}\n"
            f"本地状态：{result.local_status}\n结果：{result.result.value}\n\n"
            f"二、结论依据\n{result.summary}\n\n三、关键问题\n{important}\n"
        )
        return submitter, admin
