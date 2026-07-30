from __future__ import annotations

from typing import Any

from wiki_review_v2.models import InputCoverage, SourceDocument, VisualManifest, VisualPage
from wiki_review_v2.prompts import ReviewPromptBuilder


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
    manifest: VisualManifest | None = None,
    coverage: InputCoverage | None = None,
    attachments: list[dict[str, Any]] | None = None,
    review_mode: str = "initial",
    similarity_context: dict[str, Any] | None = None,
    rereview_context: dict[str, Any] | None = None,
) -> str:
    return ReviewPromptBuilder("审稿标准").build(
        source=_source(),
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
        "InputSemantics",
        "DocumentMetadata",
        "StructuredContent",
        "VisualInputGuide",
        "VisualManifest",
        "AttachmentGuide",
        "AttachmentMetadata",
        "InputCoverageGuide",
        "InputCoverage",
        "InitialReviewSimilarityGuide",
        "InitialReviewSimilarityContext",
        "DecisionRules",
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
        similarity_context={"threshold": 0.35, "top_n": 5, "effective_candidates": []}
    )
    for field in ("threshold", "top_n", "effective_candidates"):
        assert f"- {field}：" in prompt
    assert "score 是上游提供的候选分数" in prompt
    assert "当前工作流不负责计算该分数" in prompt
    assert "空数组表示本次没有有效候选" in prompt
    assert "不能只根据 score 直接决定合并或拒绝" in prompt
    assert "## ReReviewGuide" not in prompt


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
