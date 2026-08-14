from __future__ import annotations

import json
from typing import Any

from wiki_review_v2.models import InputCoverage, ModelReviewPayload, SourceDocument, VisualManifest, VisualPage
from wiki_review_v2.prompts import OUTPUT_JSON_EXAMPLE, ReviewPromptBuilder


def _source() -> SourceDocument:
    return SourceDocument(
        case_id="prompt-case",
        document_id="document-1",
        node_token="node-1",
        title="笔记_审稿测试-v1.0",
        wiki_name="测试知识库",
        author_id="author-1",
        author="测试用户",
        link="https://example.invalid/wiki/document-1",
    )


def _page(page: int = 1) -> VisualPage:
    return VisualPage(
        evidence_id=f"page-{page}",
        page=page,
        path=f"pages/page-{page}.png",
        width=1200,
        height=1600,
        byte_size=1024,
        sha256="a" * 64,
    )


def _build(
    *,
    source: SourceDocument | None = None,
    manifest: VisualManifest | None = None,
    coverage: InputCoverage | None = None,
    attachments: list[dict[str, Any]] | None = None,
    review_mode: str = "initial",
    similarity_context: dict[str, Any] | None = None,
    rereview_context: dict[str, Any] | None = None,
) -> str:
    return ReviewPromptBuilder("审稿标准").build(
        source=source or _source(),
        content="# 实验记录\n运行测试并观察结果。",
        manifest=manifest or VisualManifest(source="unavailable"),
        coverage=coverage
        or InputCoverage(structured_text_available=True, visual_pages_complete=False),
        review_mode=review_mode,
        similarity_context=similarity_context or {},
        rereview_context=rereview_context or {},
        attachments=[] if attachments is None else attachments,
    )


def test_prompt_sections_follow_semantics_before_data_order() -> None:
    prompt = _build()
    headings = [
        "SystemRole",
        "ReviewStandard",
        "ReviewProcedure",
        "InputSemantics",
        "DocumentMetadata",
        "StructuredContent",
        "VisualInputGuide",
        "VisualPublicationQualityRules",
        "VisualManifest",
        "AttachmentGuide",
        "AttachmentMetadata",
        "InputCoverageGuide",
        "InputCoverage",
        "InitialReviewSimilarityGuide",
        "SimilarityMergeRules",
        "InitialReviewSimilarityContext",
        "DecisionRules",
        "ArticleOverviewRequirements",
        "OutputRequirements",
    ]
    positions = [prompt.index(f"## {heading}") for heading in headings]
    assert positions == sorted(positions)
    assert "## OutputSchema" not in prompt


def test_input_coverage_guide_explains_every_field_and_constraint() -> None:
    prompt = _build(
        coverage=InputCoverage(
            structured_text_available=True,
            visual_pages_complete=False,
            attachments_opened=False,
            input_truncated=True,
            missing_sources=["visual_pages"],
            limitations=["正文超过安全上限"],
        )
    )
    for field in (
        "structured_text_available",
        "visual_pages_complete",
        "attachments_opened",
        "input_truncated",
        "missing_sources",
        "limitations",
    ):
        assert f"- {field}：" in prompt
    assert "不得假装完成全部视觉审查" in prompt
    assert "不得推测未提交的正文" in prompt
    assert "结论必须考虑输入缺失" in prompt
    assert "必须反映到 visual_evidence_assessment" in prompt


def test_visual_manifest_includes_summary_fields_and_generated_pdf_warning() -> None:
    prompt = _build(
        manifest=VisualManifest(
            source="generated_pdf",
            total_pages=2,
            rendered_pages=[_page(1)],
            failed_pages=[2],
            limitations=["第 2 页渲染失败"],
        )
    )
    for source in ("pdf", "fixture_pages", "generated_pdf", "unavailable"):
        assert source in prompt
    assert '"source": "generated_pdf"' in prompt
    assert '"total_pages": 2' in prompt
    assert '"rendered_pages"' in prompt
    assert '"failed_pages"' in prompt
    assert '"limitations"' in prompt
    assert "不能将其视为独立的原始视觉证据" in prompt
    assert "不能据此判断原始飞书排版" in prompt


def test_visual_publication_rules_reject_clipped_key_tables() -> None:
    prompt = _build(
        manifest=VisualManifest(
            source="fixture_pages",
            total_pages=1,
            rendered_pages=[_page(1)],
        ),
        coverage=InputCoverage(
            structured_text_available=True,
            visual_pages_complete=True,
            attachments_opened=False,
        ),
        attachments=[{"name": "硬件清单.xlsx", "size": 4096}],
    )
    assert "## VisualPublicationQualityRules" in prompt
    assert "被裁切、遮挡、折叠、溢出、截断" in prompt
    assert "横向/纵向滚动区域而只显示一部分" in prompt
    assert "关键表格只显示部分行或列" in prompt
    assert "major 问题，category=visibility，result=need_revision" in prompt
    assert "不得 pass，也不得降为 minor" in prompt
    assert "evidence_ids 必须引用实际显示问题的页面" in prompt
    assert "StructuredContent 中存在完整表格文字，并不能抵消" in prompt
    assert "不得假设附件可以补足页面中不可见的内容" in prompt
    assert "改为飞书原生表格" in prompt
    assert "Excel 仅作为补充附件" in prompt
    assert "按输入覆盖规则处理 incomplete_review" in prompt
    assert "generated_pdf" in prompt and "不能用于判定原始飞书页面" in prompt


def test_visual_batch_prompt_extracts_clipped_table_evidence() -> None:
    prompt = ReviewPromptBuilder("审稿标准").build_visual_batch(
        [{"evidence_id": "page-1", "page": 1}]
    )
    assert "关键表格、代码、命令、公式" in prompt
    assert "裁切、遮挡、溢出、截断" in prompt
    assert "表格只显示部分行列" in prompt
    assert "记录具体页面、可见缺陷和受影响内容" in prompt


def test_attachment_guide_distinguishes_empty_and_unopened_metadata() -> None:
    empty_prompt = _build(attachments=[])
    assert "本次 AttachmentMetadata 为空：没有附件需要打开" in empty_prompt
    assert "不表示附件输入缺失" in empty_prompt

    metadata_prompt = _build(
        attachments=[{"name": "results.zip", "size": 2048}],
        coverage=InputCoverage(
            structured_text_available=True,
            visual_pages_complete=True,
            attachments_opened=False,
        ),
    )
    assert "模型只获得附件元数据，没有读取附件实际内容" in metadata_prompt
    assert "不得根据文件名" in metadata_prompt
    assert "不能假装已经核验" in metadata_prompt


def test_initial_similarity_guide_explains_upstream_score_and_empty_candidates() -> None:
    prompt = _build(
        similarity_context={"threshold": 0.35, "top_k": 5, "effective_candidates": []}
    )
    for field in ("threshold", "top_k", "effective_candidates"):
        assert f"- {field}：" in prompt
    assert "本地确定性算法" in prompt
    assert "similarity_candidates.json 不参与召回" in prompt
    assert "AI 文章概述" in prompt
    assert "不是完整正文" in prompt
    assert "不能仅凭分数认定抄袭" in prompt
    assert "effective_candidates 为空" in prompt
    assert "## ReReviewGuide" not in prompt


def test_initial_similarity_context_contains_local_score_and_summary() -> None:
    prompt = _build(
        similarity_context={
            "threshold": 0.35,
            "top_k": 5,
            "effective_candidates": [
                {
                    "document_id": "history-1",
                    "title": "历史无人机硬件方案",
                    "wiki_name": "历史库",
                    "link": "https://example.invalid/history-1",
                    "status": "AI通过待确认",
                    "similarity_score": 0.72,
                    "score_details": {
                        "text_tfidf": 0.75,
                        "title_similarity": 0.5,
                        "topic_keyword_jaccard": 0.8,
                        "entity_jaccard": 1.0,
                        "parameter_jaccard": 0.5,
                        "final_score": 0.72,
                    },
                    "content": "标题：无人机硬件方案\n代表内容：使用 STM32H743 完成 CAN-FD 测试。",
                    "summary_source": "ai_article_overview",
                    "overview_model": "fake-kimi",
                    "overview_prompt_version": "article-overview-v1",
                }
            ],
        }
    )
    assert '"document_id": "history-1"' in prompt
    assert '"similarity_score": 0.72' in prompt
    assert "使用 STM32H743 完成 CAN-FD 测试" in prompt
    assert "不能仅凭分数认定抄袭" in prompt


def test_similarity_merge_rules_merge_repeated_drone_tutorial_workflows() -> None:
    prompt = _build(
        similarity_context={
            "threshold": 0.35,
            "top_k": 5,
            "effective_candidates": [
                {
                    "document_id": "fpv-tutorial",
                    "title": "穿越机组装搭建教程",
                    "similarity_score": 0.68,
                    "content": "包含环境搭建、QGC地面站适配、飞控连接、校准和调试流程。",
                }
            ],
        }
    )
    assert "## SimilarityMergeRules" in prompt
    assert "四旋翼无人机搭建教程" in prompt
    assert "穿越机搭建教程" in prompt
    assert "QGC 地面站安装/适配/调试" in prompt
    assert "不能以 same_area_different_direction 独立 pass" in prompt
    assert "status=merge_recommended、decision=merge_required" in prompt
    assert "result=need_revision" in prompt
    assert 'pass_reason=""' in prompt
    assert "suggested_next_action=merge_with_existing" in prompt
    assert "level=major、category=similarity" in prompt
    assert "公共教程或公共章节" in prompt
    assert "机型特有的装配、动力参数和差异步骤" in prompt
    assert "共同内容只是简短的通用前置知识" in prompt
    assert "不得仅凭同领域判合并，也不得仅凭标题不同判独立" in prompt
    assert "优先 merge_recommended" in prompt


def test_rereview_guide_requires_each_blocking_major_resolution() -> None:
    prompt = _build(
        review_mode="rereview",
        rereview_context={
            "previous_review_round": 1,
            "blocking_major_issues": [{"issue_id": "previous-1", "level": "major"}],
        },
    )
    assert "previous_review_round" in prompt
    assert "blocking_major_issues" in prompt
    assert "列表中的每一项" in prompt
    assert "resolved、partially_resolved 或 unresolved" in prompt
    assert "复审不重新召回相似候选" in prompt
    assert "不得为了延长流程而随意增加无关 minor" in prompt
    assert "## InitialReviewSimilarityContext" not in prompt
    assert "## SimilarityMergeRules" not in prompt


def test_rereview_document_metadata_does_not_repeat_unfiltered_previous_issues() -> None:
    source = _source().model_copy(
        update={
            "review_round": 1,
            "previous_issues": [
                {"issue_id": "minor-history-id", "level": "minor", "problem": "旧格式问题"}
            ],
        }
    )
    prompt = _build(
        source=source,
        review_mode="rereview",
        rereview_context={
            "previous_review_round": 1,
            "blocking_major_issues": [{"issue_id": "major-history-id", "level": "major"}],
        },
    )
    metadata = prompt.split("## DocumentMetadata\n", 1)[1].split("\n\n## StructuredContent", 1)[0]

    assert "previous_issues" not in metadata
    assert "minor-history-id" not in prompt
    assert "major-history-id" in prompt


def test_decision_and_output_requirements_explain_business_fields_safely() -> None:
    prompt = _build()
    for result in ("incomplete_review", "recommend_human_review", "reject"):
        assert result in prompt
    assert "输入不完整 → incomplete_review" in prompt
    assert "输入完整但确有资质/职责边界 → recommend_human_review" in prompt
    for field in (
        "result",
        "summary",
        "pass_reason",
        "blocking_count / major_count / minor_count",
        "issues",
        "similarity_check",
        "learning_trace_assessment",
        "visual_evidence_assessment",
        "revision_priority",
        "suggested_next_action",
        "re_review_assessment",
        "article_overview",
    ):
        assert f"- {field}：" in prompt
    assert "evidence_ids 只能引用本 Prompt 实际提供的 evidence_id" in prompt
    assert "非 pass 时必须为空字符串" in prompt
    assert "不得在 JSON 外输出解释、Markdown 围栏" in prompt
    for sensitive in (";base64,", "Authorization", "Bearer", "API Key"):
        assert sensitive not in prompt


def test_complete_visual_input_reduces_unnecessary_human_review() -> None:
    prompt = _build(
        manifest=VisualManifest(
            source="pdf",
            total_pages=2,
            rendered_pages=[_page(1), _page(2)],
        ),
        coverage=InputCoverage(
            structured_text_available=True,
            visual_pages_complete=True,
            attachments_opened=False,
        ),
    )
    assert "不得沿用 v1“图片不可读”的假设" in prompt
    assert "图片数量多" in prompt
    assert "硬件/电路" in prompt
    assert "都不能单独成为转人工复审的理由" in prompt
    assert "不得因为主题专业" in prompt
    assert "设计目标、器件或方案选择、布局/布线依据、完整设计图、测试计划和迭代方向" in prompt
    assert "不得仅因尚未给出实测数据而转人工复审" in prompt
    assert "输入完整但确有资质/职责边界 → recommend_human_review" in prompt
    assert "应直接在 pass、need_revision 或 reject 中选择" in prompt
    assert "不能单独证明内容伪造或构成直接拒稿" in prompt


def test_output_requirements_include_a_complete_schema_valid_json_example() -> None:
    validated = ModelReviewPayload.model_validate(OUTPUT_JSON_EXAMPLE)
    assert validated.result.value == "pass"
    assert set(OUTPUT_JSON_EXAMPLE) == {
        "result",
        "summary",
        "pass_reason",
        "blocking_count",
        "major_count",
        "minor_count",
        "issues",
        "similarity_check",
        "learning_trace_assessment",
        "visual_evidence_assessment",
        "revision_priority",
        "suggested_next_action",
        "re_review_assessment",
        "article_overview",
    }

    prompt = _build()
    marker = "下面是完整且语法合法的 JSON 结构示例。"
    assert marker in prompt
    example_start = prompt.index("{", prompt.index(marker))
    embedded = json.loads(prompt[example_start:])
    ModelReviewPayload.model_validate(embedded)
    assert embedded == OUTPUT_JSON_EXAMPLE


def test_output_requirements_define_types_enums_and_nested_shapes() -> None:
    prompt = _build()
    assert "14 个顶层字段" in prompt
    assert "全部必填且一个不能遗漏" in prompt
    assert "禁止输出任何额外顶层字段" in prompt
    assert "不得用 null" in prompt
    assert "issue_id(string)、level(blocking|major|minor)" in prompt
    assert "score(number, 0 到 1)" in prompt
    assert "coverage(complete|partial|unavailable)" in prompt
    assert "status(resolved|partially_resolved|unresolved)" in prompt
    assert "content(string)、topics(string[])、technical_entities(string[])、key_parameters(string[])" in prompt
    assert "merge_recommended → merge_required" in prompt
    assert "duplicate_reject_recommended → reject_independent_submission" in prompt
    assert "复审的 similarity_check 必须使用 status=not_applicable" in prompt
    assert "pass → admin_confirm" in prompt
    assert "不得因为示例是 pass 而默认 pass" in prompt
    assert '"properties"' not in prompt
    assert '"$defs"' not in prompt


def test_article_overview_is_separate_from_review_summary_and_candidates() -> None:
    prompt = _build()
    assert "## ArticleOverviewRequirements" in prompt
    assert "summary 只总结本轮审稿结论" in prompt
    assert "article_overview 只概述当前文章" in prompt
    assert "不得吸收、改写或复制 InitialReviewSimilarityContext" in prompt
    assert "目标长度 500～1200 字" in prompt
    for field in ("content", "topics", "technical_entities", "key_parameters"):
        assert field in prompt


def test_v1_review_strengths_are_adapted_to_v2_multimodal_standard(settings) -> None:
    standard = (settings.project_root / "review_standard.md").read_text(encoding="utf-8")
    for principle in (
        "不能过度放松",
        "不能无限挑刺",
        "先审内容，再审表达，最后审格式",
        "知识沉淀价值与内容完整性",
        "个人研发/学习痕迹",
        "上下文一致性与正确性",
    ):
        assert principle in standard
    for document_type in (
        "学习笔记 / 知识整理",
        "论文阅读 / 文献综述",
        "论文复现 / 代码教程 / 工程运行记录",
        "工程 SOP / 操作规范",
        "Bug 排查 / 问题记录",
        "硬件设计 / 系统方案 / 研发设计文档",
        "附件型资料文档",
    ):
        assert document_type in standard
    assert "不得沿用 v1“图片不可读”或只依赖 OCR 的假设" in standard
    assert "major 通常 1–5 条；minor 通常 0–5 条" in standard
    assert "仅披露使用或可能使用 AI 辅助写作" in standard


def test_review_procedure_prioritizes_content_evidence_and_actionable_feedback() -> None:
    prompt = _build()
    assert "## ReviewProcedure" in prompt
    assert "识别文档真实类型" in prompt
    assert "先判断知识沉淀价值、内容完整性、可理解性和个人研发/学习痕迹" in prompt
    assert "交叉核对正文与截图、图纸、表格、命令、代码、日志、结果和结论" in prompt
    assert "每条问题必须有具体位置、实际证据和可执行建议" in prompt
    assert "不得过度放松，也不得无限挑刺或滥用人工复审" in prompt
    assert "major 通常合并为 1–5 条" in prompt
    assert "建议完善内容" in prompt
    assert "category 只能是 format、structure、correctness" in prompt
