from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewOutcome(StrEnum):
    PASS = "pass"
    NEED_REVISION = "need_revision"
    RECOMMEND_HUMAN_REVIEW = "recommend_human_review"
    INCOMPLETE_REVIEW = "incomplete_review"
    REJECT = "reject"


class IssueLevel(StrEnum):
    BLOCKING = "blocking"
    MAJOR = "major"
    MINOR = "minor"


class SimilarityDecision(StrEnum):
    KEEP_INDEPENDENT = "keep_independent"
    MERGE_REQUIRED = "merge_required"
    REJECT_INDEPENDENT_SUBMISSION = "reject_independent_submission"


class SuggestedNextAction(StrEnum):
    ADMIN_CONFIRM = "admin_confirm"
    AUTHOR_REVISE = "author_revise"
    HUMAN_REVIEW = "human_review"
    HUMAN_RECHECK = "human_recheck"
    MERGE_WITH_EXISTING = "merge_with_existing"
    REJECT_INDEPENDENT_SUBMISSION = "reject_independent_submission"
    REJECT = "reject"


class SourceDocument(StrictModel):
    case_id: str
    document_id: str
    node_token: str
    title: str
    wiki_name: str
    author_id: str
    author: str
    link: str
    review_method: str = "AI"
    status: str = "AI审稿中"
    review_round: int = Field(default=0, ge=0)
    updated_at: str = ""
    last_ai_review_at: str = ""
    previous_issues: list[dict[str, Any]] = Field(default_factory=list)


class FixtureOptions(StrictModel):
    source_pdf: str | None = None
    pdf_required: bool = False
    render_fail_pages: list[int] = Field(default_factory=list)
    attachment_content_required: bool = False


class DocumentBlock(StrictModel):
    type: Literal["heading", "paragraph", "code", "table", "image", "quote", "list"]
    text: str = ""
    level: int | None = None
    language: str | None = None
    markdown: str | None = None
    caption: str | None = None


class SimilarityCandidate(StrictModel):
    document_id: str
    title: str
    wiki_name: str = ""
    link: str = ""
    status: str = "已公示"
    score: float = Field(ge=0, le=1)
    content: str = ""


class PreviousIssue(StrictModel):
    issue_id: str
    level: IssueLevel
    category: str
    problem: str
    suggestion: str = ""


class PreviousReview(StrictModel):
    schema_version: str = "2.0"
    document_id: str
    review_round: int = Field(ge=1)
    result: ReviewOutcome
    issues: list[PreviousIssue] = Field(default_factory=list)


class FixtureBundle(StrictModel):
    source_document: SourceDocument
    blocks: list[dict[str, Any]]
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    similarity_candidates: list[SimilarityCandidate] = Field(default_factory=list)
    previous_review: PreviousReview | None = None
    fixture_options: FixtureOptions = Field(default_factory=FixtureOptions)
    fake_model_response: dict[str, Any] = Field(default_factory=dict)
    expected_result: dict[str, Any] = Field(default_factory=dict)


class VisualPage(StrictModel):
    evidence_id: str
    page: int = Field(ge=1)
    path: str
    width: int
    height: int
    byte_size: int
    sha256: str


class VisualManifest(StrictModel):
    schema_version: str = "2.0"
    source: Literal["pdf", "fixture_pages", "generated_pdf", "unavailable"]
    total_pages: int = 0
    rendered_pages: list[VisualPage] = Field(default_factory=list)
    failed_pages: list[int] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class InputCoverage(StrictModel):
    schema_version: str = "2.0"
    structured_text_available: bool
    visual_pages_complete: bool
    attachments_opened: bool = False
    input_truncated: bool = False
    missing_sources: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class SimilarityProfile(StrictModel):
    schema_version: str = "1.0"
    document_id: str
    title: str
    source: Literal["blocks", "pdf", "unavailable"]
    content: str = ""
    headings: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    technical_entities: list[str] = Field(default_factory=list)
    parameters: list[str] = Field(default_factory=list)
    source_character_count: int = Field(default=0, ge=0)
    summary_character_count: int = Field(default=0, ge=0)
    limitations: list[str] = Field(default_factory=list)


class SimilarityScoreDetails(StrictModel):
    text_tfidf: float = Field(ge=0, le=1)
    title_similarity: float = Field(ge=0, le=1)
    keyword_jaccard: float = Field(ge=0, le=1)
    entity_jaccard: float = Field(ge=0, le=1)
    final_score: float = Field(ge=0, le=1)


class SimilarityScoredCandidate(StrictModel):
    document_id: str
    title: str
    wiki_name: str = ""
    link: str = ""
    status: str = ""
    score_details: SimilarityScoreDetails
    above_threshold: bool
    selected_for_prompt: bool


class SimilarityPromptCandidate(StrictModel):
    document_id: str
    title: str
    wiki_name: str = ""
    link: str = ""
    status: str = ""
    similarity_score: float = Field(ge=0, le=1)
    score_details: SimilarityScoreDetails
    content: str


class SimilarityRetrievalAudit(StrictModel):
    schema_version: str = "1.0"
    query_document_id: str
    index_candidate_count: int = Field(default=0, ge=0)
    threshold: float = Field(ge=0, le=1)
    top_k: int = Field(ge=1)
    scored_candidates: list[SimilarityScoredCandidate] = Field(default_factory=list)
    prompt_candidates: list[SimilarityPromptCandidate] = Field(default_factory=list)
    skipped_reason: str = ""


class VisualEvidence(StrictModel):
    evidence_id: str
    page: int = Field(ge=1)
    observation: str
    supports: list[str] = Field(default_factory=list)


class VisualEvidenceAssessment(StrictModel):
    coverage: Literal["complete", "partial", "unavailable"]
    pages_reviewed: list[int] = Field(default_factory=list)
    evidence_used: list[VisualEvidence] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class SimilarityCandidateAssessment(StrictModel):
    title: str
    wiki_name: str = ""
    link: str = ""
    status: str = ""
    score: float = Field(ge=0, le=1)
    relationship: Literal[
        "same_topic", "same_area_different_direction", "duplicate", "complementary", "partial_extension"
    ]
    evidence: str


class SimilarityAssessment(StrictModel):
    status: Literal[
        "no_similar",
        "same_area_different_direction",
        "related_but_keep",
        "merge_recommended",
        "duplicate_reject_recommended",
        "not_applicable",
    ]
    decision: SimilarityDecision
    summary: str
    candidate_count: int = Field(ge=0)
    candidates_considered: list[SimilarityCandidateAssessment] = Field(default_factory=list)


class ReviewIssue(StrictModel):
    issue_id: str
    level: IssueLevel
    category: str
    position: str
    problem: str
    suggestion: str
    evidence_ids: list[str] = Field(default_factory=list)


class ReReviewResolution(StrictModel):
    issue_id: str
    status: Literal["resolved", "partially_resolved", "unresolved"]
    evidence: str


class ReReviewAssessment(StrictModel):
    resolutions: list[ReReviewResolution] = Field(default_factory=list)


class ModelReviewPayload(StrictModel):
    result: ReviewOutcome
    summary: str
    pass_reason: str
    blocking_count: int = Field(ge=0)
    major_count: int = Field(ge=0)
    minor_count: int = Field(ge=0)
    issues: list[ReviewIssue]
    similarity_check: SimilarityAssessment
    learning_trace_assessment: str
    visual_evidence_assessment: VisualEvidenceAssessment
    revision_priority: list[str]
    suggested_next_action: SuggestedNextAction
    re_review_assessment: ReReviewAssessment


class ReviewResult(ModelReviewPayload):
    schema_version: str = "2.0"
    local_status: str = ""
    input_coverage: InputCoverage


class VisualEvidenceBatch(StrictModel):
    batch_summary: str
    pages_reviewed: list[int]
    evidence: list[VisualEvidence]
    limitations: list[str]


class ModelCallRecord(StrictModel):
    phase: str
    model: str
    attempt_count: int
    elapsed_ms: int
    prompt_chars: int
    image_count: int
    image_bytes: int
    finish_reason: str = ""
    token_usage: dict[str, int] = Field(default_factory=dict)


class ReviewGraphState(StrictModel):
    case_id: str
    case_path: str
    output_dir: str
    run_id: str
    thread_id: str
    model_mode: Literal["fake", "real"]
    source_document: dict[str, Any] = Field(default_factory=dict)
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    similarity_candidates: list[dict[str, Any]] = Field(default_factory=list)
    previous_review: dict[str, Any] | None = None
    fixture_options: dict[str, Any] = Field(default_factory=dict)
    fake_model_response: dict[str, Any] = Field(default_factory=dict)
    expected_result: dict[str, Any] = Field(default_factory=dict)
    extracted_content: dict[str, Any] = Field(default_factory=dict)
    visual_manifest: dict[str, Any] = Field(default_factory=dict)
    input_coverage: dict[str, Any] = Field(default_factory=dict)
    similarity_profile: dict[str, Any] = Field(default_factory=dict)
    similarity_retrieval: dict[str, Any] = Field(default_factory=dict)
    similarity_profile_persisted: bool = False
    review_mode: Literal["initial", "rereview"] = "initial"
    similarity_context: dict[str, Any] = Field(default_factory=dict)
    rereview_context: dict[str, Any] = Field(default_factory=dict)
    prompt: str = ""
    selected_pages: list[dict[str, Any]] = Field(default_factory=list)
    visual_evidence: dict[str, Any] = Field(default_factory=dict)
    raw_model_output: dict[str, Any] = Field(default_factory=dict)
    parsed_review_result: dict[str, Any] = Field(default_factory=dict)
    projected_status: str = ""
    submitter_notification: str = ""
    admin_notification: str = ""
    model_calls: list[dict[str, Any]] = Field(default_factory=list)
    request_summaries: list[dict[str, Any]] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    failure: dict[str, Any] | None = None
    technical_incomplete_reason: str = ""
