from __future__ import annotations

from ..models import ReviewResult


def render_messages(source, result: ReviewResult, status: str, *, handoff=False):
    heading = f"《{source.title}》AI审稿意见（第{source.review_round + 1}轮）\n{source.link}"
    rules = "blocking / major 必须修改；minor 通常不单独阻塞通过。复审不会因 minor 无限要求修改。"
    issues = []
    for issue in result.issues:
        if issue.level.value not in {"blocking", "major"}:
            continue
        issues.append(f"[{issue.level.value} / {issue.category}] {issue.position}\n问题：{issue.problem}\n修改建议：{issue.suggestion}")
    sections = [("审稿结论", f"当前状态：{status}\n{result.summary}"), ("问题等级说明", rules),
                ("必须修改的问题", "\n\n".join(issues) or "无 blocking / major 问题。")]
    if source.review_round == 0:
        similarity = result.similarity_check
        label = {"keep_independent": "可独立保留", "merge_required": "建议合并",
                 "reject_independent_submission": "不建议独立提交"}[similarity.decision.value]
        detail = f"处理结论：{label}\n{similarity.summary}"
        for item in similarity.candidates_considered:
            detail += f"\n{item.title}（{item.wiki_name}）\n{item.link}\n{item.evidence}"
        sections.append(("相似文档检查", detail))
    else:
        resolutions = [f"{x.issue_id}：{x.status}\n{x.evidence}" for x in result.re_review_assessment.resolutions]
        sections.append(("复审解决情况", "\n".join(resolutions) or "上一轮没有 blocking / major 待复查项。"))
    if handoff:
        sections.append(("后续处理", "已达到自动审稿轮次上限，停止自动复审，由管理员安排人工审稿。"))
    elif result.result.value in {"recommend_human_review", "incomplete_review"}:
        sections.append(("后续处理", "需要管理员安排人工审稿。当前材料或审稿覆盖的限制请结合上述意见核实。"))
    elif result.result.value == "pass":
        sections.append(("后续处理", "AI通过仍需管理员确认，不会自动公示。"))
    author = heading + "\n\n" + "\n\n".join(f"{i}. {title}\n{body}" for i, (title, body) in enumerate(sections, 1))
    admin = heading + f"\n投稿人：{source.author}\n知识库：{source.wiki_name}\n" + author.split("\n\n", 1)[1]
    return author, admin


def split_message(content: str, max_bytes: int = 12000) -> list[str]:
    """Keep paragraphs where possible, with a conservative UTF-8 byte budget."""
    result, current = [], ""
    for paragraph in content.split("\n\n"):
        candidate = (current + "\n\n" + paragraph).strip()
        if len(candidate.encode("utf-8")) <= max_bytes:
            current = candidate
            continue
        if current:
            result.append(current)
            current = ""
        for char in paragraph:
            if len((current + char).encode("utf-8")) > max_bytes:
                result.append(current)
                current = ""
            current += char
    if current:
        result.append(current)
    if len(result) > 1:
        result = [f"（{i}/{len(result)}）\n{part}" for i, part in enumerate(result, 1)]
    return result
