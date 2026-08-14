from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    position: str = ""
    problem: str
    suggestion: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


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
    schema_version: str = "2.0"
    document_id: str
    title: str
    source: Literal["blocks", "pdf", "unavailable"]
    query_text: str = ""
    headings: list[str] = Field(default_factory=list)
    local_keywords: list[str] = Field(default_factory=list)
    local_technical_entities: list[str] = Field(default_factory=list)
    local_key_parameters: list[str] = Field(default_factory=list)
    source_character_count: int = Field(default=0, ge=0)
    query_character_count: int = Field(default=0, ge=0)
    query_truncated: bool = False
    source_content_hash: str = ""
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def accept_v1_profile(cls, value: Any) -> Any:
        """Accept checkpoint/test profiles written before query-profile v2."""
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "query_text" not in data and "content" in data:
            data["query_text"] = data.pop("content")
        if "local_keywords" not in data and "keywords" in data:
            data["local_keywords"] = data.pop("keywords")
        if "local_technical_entities" not in data and "technical_entities" in data:
            data["local_technical_entities"] = data.pop("technical_entities")
        if "local_key_parameters" not in data and "parameters" in data:
            data["local_key_parameters"] = data.pop("parameters")
        if "query_character_count" not in data and "summary_character_count" in data:
            data["query_character_count"] = data.pop("summary_character_count")
        data["schema_version"] = "2.0"
        return data

    @property
    def content(self) -> str:
        return self.query_text

    @property
    def keywords(self) -> list[str]:
        return self.local_keywords

    @property
    def technical_entities(self) -> list[str]:
        return self.local_technical_entities

    @property
    def parameters(self) -> list[str]:
        return self.local_key_parameters


class SimilarityScoreDetails(StrictModel):
    text_tfidf: float = Field(ge=0, le=1)
    title_similarity: float = Field(ge=0, le=1)
    topic_keyword_jaccard: float = Field(ge=0, le=1)
    entity_jaccard: float = Field(ge=0, le=1)
    parameter_jaccard: float = Field(ge=0, le=1)
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
    entered_prompt: bool
    content: str
    summary_source: str
    overview_model: str = ""
    overview_prompt_version: str = ""


class SimilarityPromptCandidate(StrictModel):
    document_id: str
    title: str
    wiki_name: str = ""
    link: str = ""
    status: str = ""
    similarity_score: float = Field(ge=0, le=1)
    score_details: SimilarityScoreDetails
    content: str
    summary_source: str
    overview_model: str = ""
    overview_prompt_version: str = ""


class SimilarityRetrievalAudit(StrictModel):
    schema_version: str = "2.0"
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


class ArticleOverview(StrictModel):
    content: str
    topics: list[str]
    technical_entities: list[str]
    key_parameters: list[str]


class ArticleOverviewAudit(StrictModel):
    schema_version: str = "1.0"
    document_id: str
    article_overview: ArticleOverview | None = None
    summary_source: str = "ai_article_overview"
    overview_model: str = ""
    overview_prompt_version: str = "article-overview-v1"
    source_content_hash: str = ""
    validation_status: Literal["valid", "warning", "invalid", "unavailable"] = "unavailable"
    validation_warnings: list[str] = Field(default_factory=list)
    persisted_to_index: bool = False


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
    article_overview: ArticleOverview | None


class ReviewResult(ModelReviewPayload):
    schema_version: str = "2.0"
    local_status: str = ""
    input_coverage: InputCoverage
    # Old local review_history records predate article_overview.
    article_overview: ArticleOverview | None = None


class ReviewHistoryRecord(StrictModel):
    schema_version: str = "2.0"
    run_id: str = Field(min_length=1)
    review_round: int = Field(ge=1)
    completed_at: str = ""
    result: ReviewResult


class ReviewHistoryLookupAudit(StrictModel):
    schema_version: str = "2.0"
    document_id: str
    lookup_status: Literal["ok", "error"] = "ok"
    history_found: bool = False
    source: Literal["local_history", "none"] = "none"
    selected_run_id: str | None = None
    selected_review_round: int | None = None
    selected_result: ReviewOutcome | None = None
    total_history_records: int = Field(default=0, ge=0)
    blocking_major_issue_count: int = Field(default=0, ge=0)
    error_code: str | None = None


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
    previous_history_record: dict[str, Any] | None = None
    review_history_lookup: dict[str, Any] = Field(default_factory=dict)
    current_review_round: int = Field(default=1, ge=1)
    review_completed_at: str = ""
    review_history_persisted: bool = False
    fixture_options: dict[str, Any] = Field(default_factory=dict)
    fake_model_response: dict[str, Any] = Field(default_factory=dict)
    expected_result: dict[str, Any] = Field(default_factory=dict)
    extracted_content: dict[str, Any] = Field(default_factory=dict)
    visual_manifest: dict[str, Any] = Field(default_factory=dict)
    input_coverage: dict[str, Any] = Field(default_factory=dict)
    similarity_profile: dict[str, Any] = Field(default_factory=dict)
    similarity_retrieval: dict[str, Any] = Field(default_factory=dict)
    similarity_profile_persisted: bool = False
    article_overview_audit: dict[str, Any] = Field(default_factory=dict)
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
