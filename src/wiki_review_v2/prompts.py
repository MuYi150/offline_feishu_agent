from __future__ import annotations

import json
from typing import Any

from .models import InputCoverage, SourceDocument, VisualManifest


SYSTEM_ROLE = """你是科研团队知识库的质量初筛员、修改意见生成器和风险分流 Agent，不是最终管理员或最终裁判。
你具备视觉理解能力，必须实际结合正文、截图、表格、页面布局和图文关系完成审查。视觉页面完整且清晰时，图片数量多、文档依赖图片、技术主题专业或属于硬件/电路领域，都不能单独成为转人工复审的理由。截图、设计图、PCB 图、命令、配置、日志和结果都可以作为有效证据。不得凭视觉风格断言伪造或 AI 生成。无法覆盖的输入必须明确声明。只返回符合 Schema 的 JSON。"""

INPUT_SEMANTICS = """以下输入由工作流按章节提供。Guide 章节解释紧随其后的 JSON 字段，不能当作文档正文；DocumentMetadata 是文档元数据；StructuredContent 是已提取的 Markdown 正文；VisualManifest 描述本次视觉页面的来源和处理覆盖；AttachmentMetadata 描述附件；InputCoverage 汇总实际输入覆盖范围。只能依据实际提供的正文、候选、页面和附件信息作出判断，未提供的内容必须视为未知。"""

REVIEW_PROCEDURE = """必须按以下顺序完成审稿：
1. 识别文档真实类型和写作目标，不得把所有文档机械套入同一模板。
2. 先判断知识沉淀价值、内容完整性、可理解性和个人研发/学习痕迹，再检查结构、语言和标题格式。
3. 交叉核对正文与截图、图纸、表格、命令、代码、日志、结果和结论，检查是否相互支持或存在矛盾。
4. 按 blocking、major、minor 合并同类问题；每条问题必须有具体位置、实际证据和可执行建议。
5. 根据覆盖范围、问题等级、相似性和复审状态选择结论，不得过度放松，也不得无限挑刺或滥用人工复审。
6. 输出前复核问题计数、result、pass_reason、suggested_next_action、similarity_check、visual_evidence_assessment 和 re_review_assessment 是否一致，再严格按 OutputRequirements 输出 JSON。"""

VISUAL_INPUT_GUIDE = """VisualManifest 字段含义：
- source：视觉页面来源。pdf 表示页面来自 Fixture 提供的 PDF；fixture_pages 表示页面来自 Fixture 直接提供的 PNG/JPG 等页面图片；generated_pdf 表示页面由结构化 Markdown 自动生成；unavailable 表示没有可用视觉页面。
- total_pages：当前视觉来源声明的总页数。
- rendered_pages：成功处理且可通过 evidence_id 引用的页面；其中 page 是一基页码，width/height 是渲染图像尺寸。
- failed_pages：未成功处理的页码；非空时不得声称完成全部视觉审查。
- limitations：视觉输入处理过程中必须在结论中承认的限制。
当 source 为 pdf 或 fixture_pages、visual_pages_complete=true 且页面实际清晰可读时，应把页面中的图片、图纸、截图、标注、表格和排版信息作为已经审查的内容；不得沿用 v1“图片不可读”的假设，也不得仅因图片多或正文依赖图片而输出 recommend_human_review。
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
- threshold：本地候选进入本次模型比较的最低相似分数。
- top_k：最多提交给模型的候选数量。
- effective_candidates：从本地 SQLite 历史摘要索引计算、排除当前 document_id、经过阈值和 Top-K 筛选后的候选；Fixture 的 similarity_candidates.json 不参与召回。
- similarity_score：本地确定性算法根据摘要字符 TF-IDF、标题、关键词和技术实体计算的 0～1 分数，不是模型生成的结论。
- score_details：上述四项分数及 final_score，输出 candidates_considered 时应把 similarity_score 对应填写到 score 字段。
- content：历史文章的可读文字摘要，不是完整正文，也不包含历史 PDF、PNG 或页面图片。
每个候选同时提供 document_id、标题、链接、状态、分数和摘要。算法分数只表示可能相似，不能仅凭分数认定抄袭、重复、必须合并或拒稿。必须结合当前文章正文和候选摘要，分别判断是否主题相似但内容独立、是否明显重复、是否需要作者修改，并写明判断依据。
effective_candidates 为空表示本次没有达到阈值的本地候选，不允许凭空声称存在重复文章。"""

REREVIEW_GUIDE = """ReReviewHistoryContext 字段含义：
- previous_review_round：上一轮审稿轮次。
- blocking_major_issues：上一轮需要重点复查的 blocking/major 问题。
复审必须针对列表中的每一项，根据当前正文和证据判断 resolved、partially_resolved 或 unresolved，并写入 re_review_assessment。resolutions 必须完整且仅覆盖上述 blocking/major issue_id，不得遗漏、重复或添加无关 issue_id。复审不重新召回相似候选。不得为了延长流程而随意增加无关 minor。"""

DECISION_RULES = """决定规则：
- blocking：严重问题，但不一定直接拒稿。只有明确属于直接拒稿条件的 blocking 才允许 result=reject；其他可修正 blocking 应为 need_revision。
- major：影响正确性、可复现性、内容充分度或结构，需要作者修改，通常对应 need_revision。
- minor：轻微格式、标题或表达问题；只有 minor 或没有问题时原则上仍可 pass。
- reject：只用于明确的直接拒稿类 blocking，不能仅因存在一般 blocking、major、图片、表格或附件而使用。
- incomplete_review：输入覆盖不足，无法形成可靠的通过或拒稿结论。输入不完整时使用该结果。
- recommend_human_review：输入完整，并且存在必须由具备特定资质或职责的人员作最终判断的明确事项，例如正式安全认证、法规合规结论或高风险专业审批。不得用它代替输入缺失，也不得因为主题专业、模型希望更谨慎、包含硬件/电路内容或图片数量较多而使用。
- pass：正文与可见页面共同形成完整、连贯、可理解的内容，没有 blocking/major 时应正常通过。设计类文档可由清晰的设计目标、器件或方案选择、布局/布线依据、完整设计图、测试计划和迭代方向构成充分研发痕迹；若文档定位是设计方案而非已完成测试报告，不得仅因尚未给出实测数据而转人工复审。
必须严格区分“输入不完整 → incomplete_review”和“输入完整但确有资质/职责边界 → recommend_human_review”。视觉输入完整且模型能够依据正文和页面作出质量判断时，应直接在 pass、need_revision 或 reject 中选择。表格标签【表格】代表已提取内容，不是不可见占位符。仅披露使用或可能使用 AI 辅助写作，不能单独证明内容伪造或构成直接拒稿；必须依据实际内容、研发痕迹和证据判断。相似性 decision 必须与 status 保持一致。"""

OUTPUT_JSON_EXAMPLE: dict[str, Any] = {
    "result": "pass",
    "summary": "结构示例：请替换为本次审稿结论和依据。",
    "pass_reason": "结构示例：result=pass 时填写具体通过理由。",
    "blocking_count": 0,
    "major_count": 0,
    "minor_count": 0,
    "issues": [],
    "similarity_check": {
        "status": "no_similar",
        "decision": "keep_independent",
        "summary": "结构示例：请替换为本次相似性结论。",
        "candidate_count": 0,
        "candidates_considered": [],
    },
    "learning_trace_assessment": "结构示例：请替换为本次研发或学习痕迹判断。",
    "visual_evidence_assessment": {
        "coverage": "complete",
        "pages_reviewed": [],
        "evidence_used": [],
        "limitations": [],
    },
    "revision_priority": [],
    "suggested_next_action": "admin_confirm",
    "re_review_assessment": {"resolutions": []},
}

OUTPUT_REQUIREMENTS = """JSON Schema 由 API 的 response_format 单独提供；下面给出与该 Schema 一致的完整合法 JSON 要求和结构示例。

格式硬约束：
1. 最终响应必须是单个、可被标准 JSON 解析器直接解析的对象；只能使用双引号，不得使用单引号、注释、尾随逗号、NaN 或 Infinity。
2. 必须输出以下 13 个顶层字段，全部必填且一个不能遗漏：result、summary、pass_reason、blocking_count、major_count、minor_count、issues、similarity_check、learning_trace_assessment、visual_evidence_assessment、revision_priority、suggested_next_action、re_review_assessment。
3. 禁止输出任何额外顶层字段，尤其不要输出 schema_version、local_status、input_coverage、reasoning、analysis 或 markdown。
4. 字符串必须是 JSON string，数量和页码必须是 JSON integer，数组即使为空也必须写成 []，对象即使内容为空也必须保留其全部必填字段；不得用 null 代替字符串、数组或对象。
5. 只输出 JSON 对象本身，不得在 JSON 外输出解释、Markdown 围栏、标题、前缀或后缀文字。

关键输出字段的业务含义：
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

问题意见质量要求：列出全部关键 blocking；major 通常合并为 1–5 条；minor 通常 0–5 条。不得逐字逐句罗列重复问题，不得用“建议完善内容”“建议优化格式”等空泛表述。position、problem、suggestion 必须分别回答“具体在哪里”“实际有什么问题及证据”“作者应如何修改”。如果判断缺少研发/学习痕迹，必须列出已看到的证据和仍缺少的证据；如果使用视觉证据，必须说明对应页面实际显示了什么。

嵌套对象必须使用以下完整字段：
- issues 中每个元素必须且只能包含：issue_id(string)、level(blocking|major|minor)、category、position(string)、problem(string)、suggestion(string)、evidence_ids(string[])。category 只能是 format、structure、correctness、reproducibility、safety、placement、similarity、visibility、learning_trace、content_sufficiency、content_placeholder、visual_content_missing、sensitive_content_review、ai_generation_artifact、ai_generated_without_personal_trace、ai_revision_padding、fabricated_content 或 other。
- similarity_check 必须且只能包含：status、decision、summary、candidate_count、candidates_considered。candidate_count 必须等于 candidates_considered 的实际元素数。
- candidates_considered 中每个元素必须且只能包含：title(string)、wiki_name(string)、link(string)、status(string)、score(number, 0 到 1)、relationship、evidence(string)。relationship 只能是 same_topic、same_area_different_direction、duplicate、complementary 或 partial_extension。
- visual_evidence_assessment 必须且只能包含：coverage(complete|partial|unavailable)、pages_reviewed(integer[])、evidence_used、limitations(string[])。
- evidence_used 中每个元素必须且只能包含：evidence_id(string)、page(integer 且从 1 开始)、observation(string)、supports(string[])；evidence_id 只能引用实际提供的页面证据。
- re_review_assessment 必须且只能包含 resolutions。resolutions 中每个元素必须且只能包含：issue_id(string)、status(resolved|partially_resolved|unresolved)、evidence(string)。

相似性 status 与 decision 必须严格配对：
- no_similar、same_area_different_direction、related_but_keep、not_applicable → keep_independent；
- merge_recommended → merge_required；
- duplicate_reject_recommended → reject_independent_submission。
复审的 similarity_check 必须使用 status=not_applicable、decision=keep_independent、candidate_count=0、candidates_considered=[]。

suggested_next_action 只能是 admin_confirm、author_revise、human_review、human_recheck、merge_with_existing、reject_independent_submission 或 reject，并与结论一致：pass → admin_confirm；need_revision → author_revise；recommend_human_review → human_review；incomplete_review → human_recheck；reject → reject。相似性要求合并或不建议独立提交时，分别使用 merge_with_existing 或 reject_independent_submission。

下面是完整且语法合法的 JSON 结构示例。它只用于展示字段、类型和嵌套结构，示例中的结论与文字不是本次审稿事实，必须根据本次输入替换；不得因为示例是 pass 而默认 pass：""" + "\n" + json.dumps(
    OUTPUT_JSON_EXAMPLE,
    ensure_ascii=False,
    indent=2,
)


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
            ("ReviewProcedure", REVIEW_PROCEDURE),
            ("InputSemantics", INPUT_SEMANTICS),
            (
                "DocumentMetadata",
                json.dumps(source.model_dump(mode="json", exclude={"previous_issues"}), ensure_ascii=False, indent=2),
            ),
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
