from __future__ import annotations

import json
from typing import Any

from .models import InputCoverage, SourceDocument, VisualManifest


SYSTEM_ROLE = """你是科研团队知识库的质量初筛与风险分流 Agent，不是最终管理员。
必须同时审查正文、截图、表格、页面布局和图文关系。不得因为存在图片或表格直接拒稿；截图中的命令、配置、日志和结果可以作为实践证据。不得凭视觉风格断言伪造或 AI 生成。无法覆盖的输入必须明确声明。只返回符合 Schema 的 JSON。"""

INPUT_SEMANTICS = """以下输入由工作流按章节提供。Guide 章节解释紧随其后的 JSON 字段，不能当作文档正文；DocumentMetadata 是文档元数据；StructuredContent 是已提取的 Markdown 正文；VisualManifest 描述本次视觉页面的来源和处理覆盖；AttachmentMetadata 描述附件；InputCoverage 汇总实际输入覆盖范围。只能依据实际提供的正文、候选、页面和附件信息作出判断，未提供的内容必须视为未知。"""

VISUAL_INPUT_GUIDE = """VisualManifest 字段含义：
- source：视觉页面来源。pdf 表示页面来自 Fixture 提供的 PDF；fixture_pages 表示页面来自 Fixture 直接提供的 PNG/JPG 等页面图片；generated_pdf 表示页面由结构化 Markdown 自动生成；unavailable 表示没有可用视觉页面。
- total_pages：当前视觉来源声明的总页数。
- rendered_pages：成功处理且可通过 evidence_id 引用的页面；其中 page 是一基页码，width/height 是渲染图像尺寸。
- failed_pages：未成功处理的页码；非空时不得声称完成全部视觉审查。
- limitations：视觉输入处理过程中必须在结论中承认的限制。
特别注意：generated_pdf 页面由结构化正文生成，内容可能与 StructuredContent 重复，不能将其视为独立的原始视觉证据，也不能据此判断原始飞书排版。"""

INPUT_COVERAGE_GUIDE = """InputCoverage 字段含义：
- structured_text_available：是否取得可供审查的结构化 Markdown 正文。
- visual_pages_complete：当前要求处理的视觉页面是否全部成功处理；false 表示至少存在缺页、失败页或视觉页面不可用。
- attachments_opened：是否打开了附件的实际内容，不表示附件是否存在。
- input_truncated：结构化正文是否因超过安全上限而没有完整提交。
- missing_sources：哪些必要输入来源没有提供给模型。
- limitations：本次自动审稿必须明确声明的具体限制。
解释规则：
1. attachments_opened=false 且 AttachmentMetadata 为空，表示没有附件需要打开，不构成附件输入缺失。
2. attachments_opened=false 且 AttachmentMetadata 非空，表示只提供了附件元数据，没有读取附件实际内容。
3. visual_pages_complete=false 时，不得假装完成全部视觉审查。
4. input_truncated=true 时，不得推测未提交的正文。
5. missing_sources 非空时，结论必须考虑输入缺失。
6. limitations 中的内容必须反映到 visual_evidence_assessment 或 summary、pass_reason 等最终审稿说明中。"""

INITIAL_REVIEW_SIMILARITY_GUIDE = """InitialReviewSimilarityContext 字段含义：
- threshold：上游候选进入本次比较的最低相似分数。
- top_n：最多提交给模型的候选数量。
- effective_candidates：经过状态、正文、分数和当前文档排除规则筛选后的有效候选。
- effective_candidates 中的 score 是上游提供的候选分数，当前工作流不负责计算该分数。
空数组表示本次没有有效候选，不允许凭空声称存在重复文章。必须结合候选正文判断关系，不能只根据 score 直接决定合并或拒绝。"""

REREVIEW_GUIDE = """ReReviewHistoryContext 字段含义：
- previous_review_round：上一轮审稿轮次。
- blocking_major_issues：上一轮需要重点复查的 blocking/major 问题。
复审必须针对列表中的每一项，根据当前正文和证据判断 resolved、partially_resolved 或 unresolved，并写入 re_review_assessment。复审不重新召回相似候选。不得为了延长流程而随意增加无关 minor。"""

DECISION_RULES = """决定规则：
- blocking：严重问题，但不一定直接拒稿。只有明确属于直接拒稿条件的 blocking 才允许 result=reject；其他可修正 blocking 应为 need_revision。
- major：影响正确性、可复现性、内容充分度或结构，需要作者修改，通常对应 need_revision。
- minor：轻微格式、标题或表达问题；只有 minor 或没有问题时原则上仍可 pass。
- reject：只用于明确的直接拒稿类 blocking，不能仅因存在一般 blocking、major、图片、表格或附件而使用。
- incomplete_review：输入覆盖不足，无法形成可靠的通过或拒稿结论。输入不完整时使用该结果。
- recommend_human_review：输入完整，但问题需要专业人员判断。不得用它代替输入缺失场景。
必须严格区分“输入不完整 → incomplete_review”和“输入完整但需要专家判断 → recommend_human_review”。表格标签【表格】代表已提取内容，不是不可见占位符。相似性 decision 必须与 status 保持一致。"""

OUTPUT_REQUIREMENTS = """JSON Schema 由 API 的 response_format 单独提供，此处不重复粘贴。关键输出字段的业务含义：
- result：最终分流结果，只能是 pass、need_revision、reject、incomplete_review 或 recommend_human_review。
- summary：对本轮审稿结论和核心依据的简明总结，并反映必要的输入限制。
- pass_reason：通过理由；result=pass 时必须明确填写，非 pass 时必须为空字符串。
- blocking_count / major_count / minor_count：各等级问题数量，必须与 issues 中实际等级逐项统计一致。
- issues：具体问题列表。每个 issue 的 position 必须指出正文、页码或元数据中的具体位置；problem 只描述问题本身；suggestion 给出可执行的修改建议；evidence_ids 只能引用本 Prompt 实际提供的 evidence_id。
- similarity_check：首审时记录有效候选的比较结论；复审时使用 not_applicable，不得重新虚构候选。
- learning_trace_assessment：说明个人实践、操作过程、观察、输出和结论是否充分。
- visual_evidence_assessment：如实记录视觉覆盖、已审页码、使用的视觉证据和限制；不得声称审查了失败页或未提供页面。
- revision_priority：按优先顺序列出作者应处理的关键修改；无须修改时为空数组。
- suggested_next_action：与 result 和相似性结论一致的下一步动作。
- re_review_assessment：复审时逐项记录上一轮 blocking/major 的解决状态；首审时保持空 resolutions。
只输出一个符合 response_format Schema 的 JSON 对象。不得在 JSON 外输出解释、Markdown 围栏、标题、前缀或后缀文字。"""


def _attachment_guide(attachments: list[dict[str, Any]], coverage: InputCoverage) -> str:
    base = """AttachmentMetadata 本节提供的是附件元数据列表，不是附件实际内容。attachments_opened 表示工作流是否另外打开了附件实际内容。
- 空数组表示没有附件。
- 非空数组且 attachments_opened=false，表示只看到了附件元数据，附件内容没有被读取。
- 不得根据文件名、扩展名或其他元数据猜测附件内容。
- 如果关键结论依赖未打开的附件，必须在 visual_evidence_assessment 或最终审稿说明中声明限制，不能假装已经核验。"""
    if not attachments:
        current = "本次 AttachmentMetadata 为空：没有附件需要打开；即使 attachments_opened=false，也不表示附件输入缺失。"
    elif coverage.attachments_opened:
        current = "本次 AttachmentMetadata 非空且 attachments_opened=true：工作流声明附件实际内容已打开；本节 JSON 仍只列元数据。"
    else:
        current = "本次 AttachmentMetadata 非空且 attachments_opened=false：模型只获得附件元数据，没有读取附件实际内容。"
    return f"{base}\n{current}"


def _visual_manifest_payload(manifest: VisualManifest) -> dict[str, Any]:
    return {
        "source": manifest.source,
        "total_pages": manifest.total_pages,
        "rendered_pages": [
            {
                "evidence_id": page.evidence_id,
                "page": page.page,
                "width": page.width,
                "height": page.height,
            }
            for page in manifest.rendered_pages
        ],
        "failed_pages": manifest.failed_pages,
        "limitations": manifest.limitations,
    }


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
        sections: list[tuple[str, str]] = [
            ("SystemRole", SYSTEM_ROLE),
            ("ReviewStandard", self.review_standard),
            ("InputSemantics", INPUT_SEMANTICS),
            ("DocumentMetadata", json.dumps(source.model_dump(mode="json"), ensure_ascii=False, indent=2)),
            ("StructuredContent", content),
            ("VisualInputGuide", VISUAL_INPUT_GUIDE),
            ("VisualManifest", json.dumps(_visual_manifest_payload(manifest), ensure_ascii=False, indent=2)),
        ]
        if visual_evidence:
            sections.append(("BatchedVisualEvidence", json.dumps(visual_evidence, ensure_ascii=False, indent=2)))
        sections.extend(
            [
                ("AttachmentGuide", _attachment_guide(attachments, coverage)),
                ("AttachmentMetadata", json.dumps(attachments, ensure_ascii=False, indent=2)),
                ("InputCoverageGuide", INPUT_COVERAGE_GUIDE),
                ("InputCoverage", json.dumps(coverage.model_dump(mode="json"), ensure_ascii=False, indent=2)),
            ]
        )
        if review_mode == "initial":
            sections.extend(
                [
                    ("InitialReviewSimilarityGuide", INITIAL_REVIEW_SIMILARITY_GUIDE),
                    (
                        "InitialReviewSimilarityContext",
                        json.dumps(similarity_context, ensure_ascii=False, indent=2),
                    ),
                ]
            )
        else:
            sections.extend(
                [
                    ("ReReviewGuide", REREVIEW_GUIDE),
                    ("ReReviewHistoryContext", json.dumps(rereview_context, ensure_ascii=False, indent=2)),
                ]
            )
        sections.extend(
            [
                ("DecisionRules", DECISION_RULES),
                ("OutputRequirements", OUTPUT_REQUIREMENTS),
            ]
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
