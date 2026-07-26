from __future__ import annotations

import json
from typing import Any

from .models import InputCoverage, SourceDocument, VisualManifest


SYSTEM_ROLE = """你是科研团队知识库的质量初筛与风险分流 Agent，不是最终管理员。
必须同时审查正文、截图、表格、页面布局和图文关系。不得因为存在图片或表格直接拒稿；截图中的命令、配置、日志和结果可以作为实践证据。不得凭视觉风格断言伪造或 AI 生成。无法覆盖的输入必须明确声明。只返回符合 Schema 的 JSON。"""

DECISION_RULES = """决定规则：只有直接拒稿类 blocking 才能 reject；可修改 blocking/major 为 need_revision；只有 minor 或无问题原则上 pass；技术输入不完整使用 incomplete_review；需要专业人工判断使用 recommend_human_review。表格标签【表格】代表已提取内容，不是不可见占位符。相似性 decision 必须与 status 固定映射。"""


class ReviewPromptBuilder:
    def __init__(self, review_standard: str) -> None:
        self.review_standard = review_standard

    def build(
        self,
        *,
        source: SourceDocument,
        content: str,
        manifest: VisualManifest,
        coverage: InputCoverage,
        review_mode: str,
        similarity_context: dict[str, Any],
        rereview_context: dict[str, Any],
        attachments: list[dict[str, Any]],
        visual_evidence: dict[str, Any] | None = None,
    ) -> str:
        sections = [
            ("SystemRole", SYSTEM_ROLE),
            ("ReviewStandard", self.review_standard),
            ("DocumentMetadata", json.dumps(source.model_dump(mode="json"), ensure_ascii=False, indent=2)),
            ("StructuredContent", content),
            (
                "VisualManifest",
                json.dumps(
                    [
                        {"evidence_id": page.evidence_id, "page": page.page, "width": page.width, "height": page.height}
                        for page in manifest.rendered_pages
                    ],
                    ensure_ascii=False,
                    indent=2,
                ),
            ),
            ("AttachmentMetadata", json.dumps(attachments, ensure_ascii=False, indent=2)),
            ("InputCoverage", json.dumps(coverage.model_dump(mode="json"), ensure_ascii=False, indent=2)),
            (
                "InitialReviewSimilarityContext",
                json.dumps(similarity_context, ensure_ascii=False, indent=2)
                if review_mode == "initial"
                else "复审不重新召回相似候选。",
            ),
            (
                "ReReviewHistoryContext",
                json.dumps(rereview_context, ensure_ascii=False, indent=2)
                if review_mode == "rereview"
                else "本次为第一轮审稿。",
            ),
            ("DecisionRules", DECISION_RULES),
        ]
        if visual_evidence:
            sections.append(("BatchedVisualEvidence", json.dumps(visual_evidence, ensure_ascii=False, indent=2)))
        sections.append(
            (
                "OutputSchema",
                "严格按提供的 JSON Schema 输出。首轮必须给出明确相似性结论；复审逐项判断历史 blocking/major，且不得无限新增 minor。视觉判断尽量引用 evidence_id 和页码。",
            )
        )
        return "\n\n".join(f"## {name}\n{value}" for name, value in sections)

    def build_visual_batch(self, pages: list[dict[str, Any]]) -> str:
        listing = [{"evidence_id": page["evidence_id"], "page": page["page"]} for page in pages]
        return (
            f"{SYSTEM_ROLE}\n\n"
            "仅提取这些页面中与学习痕迹、正确性、可复现性、截图命令/配置/日志/结果有关的视觉证据。"
            "不得作最终审稿结论。每条证据引用 evidence_id 和 page。\n\n"
            + json.dumps(listing, ensure_ascii=False, indent=2)
        )

