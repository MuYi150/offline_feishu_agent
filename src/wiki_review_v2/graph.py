from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from .config import Settings
from .errors import ModelJsonError, ModelSchemaError, ReviewError, ReviewHistoryError, classify_exception
from .fixtures import DocumentExtractor, FixtureDocumentSource
from .model import ModelRequest, ReviewModel, request_summary
from .models import (
    InputCoverage,
    FixtureOptions,
    ModelReviewPayload,
    PreviousReview,
    ReviewGraphState,
    ReviewHistoryLookupAudit,
    ReviewHistoryRecord,
    ReviewResult,
    SimilarityProfile,
    SimilarityRetrievalAudit,
    SourceDocument,
    VisualEvidenceBatch,
    VisualManifest,
)
from .notifications import NotificationRenderer
from .pdf import PdfPageRenderer, calculate_input_coverage
from .policies import ReviewHistoryPolicy, ReviewModePolicy
from .prompts import ReviewPromptBuilder
from .similarity_index import LocalSimilarityRetriever, SimilarityIndexStore
from .similarity_profile import SimilarityProfileBuilder
from .result import (
    ReviewOutcomeMapper,
    ReviewResultNormalizer,
    ReviewResultParser,
    ReviewResultValidator,
    incomplete_payload,
)
from .storage import (
    AuditStore,
    ReviewHistoryStore,
    atomic_replace_json,
    atomic_write_json,
    atomic_write_text,
)


def _event(node: str, status: str, **details: Any) -> dict[str, Any]:
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "node": node,
        "status": status,
        "details": details,
    }


def _get(state: ReviewGraphState | dict[str, Any], key: str) -> Any:
    return getattr(state, key) if isinstance(state, ReviewGraphState) else state.get(key)


def _trace(state: ReviewGraphState | dict[str, Any]) -> list[dict[str, Any]]:
    return list(_get(state, "trace") or [])


class ReviewWorkflow:
    def __init__(self, settings: Settings, model: ReviewModel) -> None:   
        self.settings = settings                                                  #初始化组件
        self.model = model
        self.fixture_source = FixtureDocumentSource()
        self.extractor = DocumentExtractor()
        self.pdf = PdfPageRenderer(settings)
        self.mode_policy = ReviewModePolicy()
        self.similarity_profiles = SimilarityProfileBuilder(settings)
        self.similarity_index = SimilarityIndexStore(settings.similarity_index_path)
        self.similarity = LocalSimilarityRetriever(settings, self.similarity_index)
        self.history_policy = ReviewHistoryPolicy()
        standard_path = settings.project_root / "review_standard.md"
        standard = standard_path.read_text(encoding="utf-8") if standard_path.exists() else "按科研知识库最低质量标准审稿。"
        self.prompt_builder = ReviewPromptBuilder(standard)
        self.parser = ReviewResultParser()
        self.normalizer = ReviewResultNormalizer()
        self.validator = ReviewResultValidator()
        self.mapper = ReviewOutcomeMapper()
        self.notifications = NotificationRenderer()
        self.audit = AuditStore()
        self.history = ReviewHistoryStore(settings.state_root)

    def compile(self, checkpointer: Any) -> Any:                                    #创建 StateGraph 图
        builder = StateGraph(ReviewGraphState)

        nodes: dict[str, Callable[[ReviewGraphState], dict[str, Any]]] = {
            "load_fixture": self._guard("load_fixture", self.load_fixture),
            "extract_document": self._guard("extract_document", self.extract_document),
            "load_review_history": self._guard("load_review_history", self.load_review_history),
            "build_similarity_profile": self._guard(
                "build_similarity_profile", self.build_similarity_profile
            ),
            "prepare_pdf_pages": self._guard("prepare_pdf_pages", self.prepare_pdf_pages),
            "build_input_coverage": self._guard("build_input_coverage", self.build_input_coverage),
            "route_review_mode": self._guard("route_review_mode", self.route_review_mode),
            "retrieve_similar_documents": self._guard(
                "retrieve_similar_documents", self.retrieve_similar_documents
            ),
            "load_previous_issues": self._guard("load_previous_issues", self.load_previous_issues),
            "build_multimodal_request": self._guard(
                "build_multimodal_request", self.build_multimodal_request
            ),
            "invoke_kimi": self._guard("invoke_kimi", self.invoke_kimi),
            "parse_result": self._guard("parse_result", self.parse_result),
            "normalize_result": self._guard("normalize_result", self.normalize_result),
            "validate_result": self._guard("validate_result", self.validate_result),
            "project_status": self._guard("project_status", self.project_status),
            "render_notifications": self._guard("render_notifications", self.render_notifications),
            "save_artifacts": self._guard("save_artifacts", self.save_artifacts),
            "persist_review_history": self._guard(
                "persist_review_history", self.persist_review_history
            ),
            "persist_similarity_profile": self._guard(
                "persist_similarity_profile", self.persist_similarity_profile
            ),
            "record_safe_failure": self.record_safe_failure,
        }
        for name, node in nodes.items():
            builder.add_node(name, node)                                           #创建节点
        builder.add_edge(START, "load_fixture")                                    #注册条件路线
        ordinary = [
            ("load_fixture", "extract_document"),
            ("extract_document", "load_review_history"),
            ("load_review_history", "build_similarity_profile"),
            ("build_similarity_profile", "prepare_pdf_pages"),
            ("prepare_pdf_pages", "build_input_coverage"),
            ("build_input_coverage", "route_review_mode"),
        ]
        for current, nxt in ordinary: #首审复审条件边
            builder.add_conditional_edges(current, self._failure_route, {"ok": nxt, "failure": "record_safe_failure"})
        builder.add_conditional_edges(
            "route_review_mode",
            self._mode_route,
            {
                "initial": "retrieve_similar_documents",
                "rereview": "load_previous_issues",
                "failure": "record_safe_failure",
            },
        )
        builder.add_conditional_edges(
            "retrieve_similar_documents",
            self._failure_route,
            {"ok": "build_multimodal_request", "failure": "record_safe_failure"},
        )
        builder.add_conditional_edges(
            "load_previous_issues",
            self._failure_route,
            {"ok": "build_multimodal_request", "failure": "record_safe_failure"},
        )
        tail = [
            ("build_multimodal_request", "invoke_kimi"),
            ("invoke_kimi", "parse_result"),
            ("parse_result", "normalize_result"),
            ("normalize_result", "validate_result"),
            ("validate_result", "project_status"),
            ("project_status", "render_notifications"),
            ("render_notifications", "save_artifacts"),
        ]
        for current, nxt in tail:
            builder.add_conditional_edges(current, self._failure_route, {"ok": nxt, "failure": "record_safe_failure"})
        builder.add_conditional_edges(
            "save_artifacts",
            self._failure_route,
            {"ok": "persist_review_history", "failure": "record_safe_failure"},
        )
        builder.add_conditional_edges(
            "persist_review_history",
            self._failure_route,
            {"ok": "persist_similarity_profile", "failure": "record_safe_failure"},
        )
        builder.add_conditional_edges(
            "persist_similarity_profile",
            self._failure_route,
            {"ok": END, "failure": "record_safe_failure"},
        )
        builder.add_edge("record_safe_failure", END)
        return builder.compile(checkpointer=checkpointer)

    def _guard(
        self, node_name: str, function: Callable[[ReviewGraphState], dict[str, Any]]
    ) -> Callable[[ReviewGraphState], dict[str, Any]]:
        def guarded(state: ReviewGraphState) -> dict[str, Any]:
            try:
                update = function(state)
                update["trace"] = list(update.get("trace", _trace(state))) + [_event(node_name, "completed")]
                return update
            except BaseException as exc:
                return {
                    "failure": classify_exception(exc),
                    "trace": _trace(state) + [_event(node_name, "failed", error=classify_exception(exc))],
                }

        return guarded

    @staticmethod
    def _failure_route(state: ReviewGraphState) -> str:
        return "failure" if _get(state, "failure") else "ok"

    @staticmethod
    def _mode_route(state: ReviewGraphState) -> str:
        if _get(state, "failure"):
            return "failure"
        return str(_get(state, "review_mode"))

    def load_fixture(self, state: ReviewGraphState) -> dict[str, Any]:                          #第一个业务节点
        bundle = self.fixture_source.load(Path(state.case_path))
        return {
            "source_document": bundle.source_document.model_dump(mode="json"),
            "blocks": bundle.blocks,
            "attachments": bundle.attachments,
            "similarity_candidates": [item.model_dump(mode="json") for item in bundle.similarity_candidates],
            # Fixture history remains parseable for v1 compatibility, but production routing
            # is exclusively driven by ReviewHistoryStore in load_review_history.
            "previous_review": None,
            "fixture_options": bundle.fixture_options.model_dump(mode="json"),
            "fake_model_response": bundle.fake_model_response,
            "expected_result": bundle.expected_result,
        }

    def extract_document(self, state: ReviewGraphState) -> dict[str, Any]:                      #第二个业务节点   
        extracted = self.extractor.extract(state.blocks)                                        #Blocks 转换markdown
        return {"extracted_content": extracted}

    def load_review_history(self, state: ReviewGraphState) -> dict[str, Any]:
        source = SourceDocument.model_validate(state.source_document)
        try:
            latest = self.history.load_latest(source.document_id)
            total = self.history.record_count(source.document_id)
        except ReviewHistoryError:
            audit = ReviewHistoryLookupAudit(
                document_id=source.document_id,
                lookup_status="error",
                error_code=ReviewHistoryError.code,
            )
            atomic_replace_json(
                Path(state.output_dir) / "review_history_lookup.json",
                audit.model_dump(mode="json"),
            )
            raise

        previous = None
        blocking_major_count = 0
        if latest is not None:
            previous = PreviousReview(
                document_id=source.document_id,
                review_round=latest.review_round,
                result=latest.result.result,
                issues=[
                    issue.model_dump(mode="json")
                    for issue in latest.result.issues
                ],
            )
            blocking_major_count = sum(
                issue.level.value in {"blocking", "major"} for issue in previous.issues
            )
        audit = ReviewHistoryLookupAudit(
            document_id=source.document_id,
            history_found=latest is not None,
            source="local_history" if latest is not None else "none",
            selected_run_id=latest.run_id if latest else None,
            selected_review_round=latest.review_round if latest else None,
            selected_result=latest.result.result if latest else None,
            total_history_records=total,
            blocking_major_issue_count=blocking_major_count,
        )
        atomic_replace_json(
            Path(state.output_dir) / "review_history_lookup.json",
            audit.model_dump(mode="json"),
        )
        return {
            "previous_review": previous.model_dump(mode="json") if previous else None,
            "previous_history_record": latest.model_dump(mode="json") if latest else None,
            "review_history_lookup": audit.model_dump(mode="json"),
            "current_review_round": (latest.review_round + 1) if latest else 1,
        }

    def build_similarity_profile(self, state: ReviewGraphState) -> dict[str, Any]:
        profile = self.similarity_profiles.build(
            source=SourceDocument.model_validate(state.source_document),
            extracted_content=state.extracted_content,
            case_path=Path(state.case_path),
            options=FixtureOptions.model_validate(state.fixture_options),
        )
        return {"similarity_profile": profile.model_dump(mode="json")}

    def prepare_pdf_pages(self, state: ReviewGraphState) -> dict[str, Any]:                     #第三个
        source = SourceDocument.model_validate(state.source_document)  #PDF渲染需要的源文档信息
        options = FixtureOptions.model_validate(state.fixture_options)
        manifest, reason = self.pdf.prepare(              #pdf渲染
            Path(state.case_path),
            Path(state.output_dir),
            source,
            options,
            str(state.extracted_content.get("content_markdown", "")),
        )
        return {"visual_manifest": manifest.model_dump(mode="json"), "technical_incomplete_reason": reason}

    def build_input_coverage(self, state: ReviewGraphState) -> dict[str, Any]: #交给模型的材料是否完善
        source = SourceDocument.model_validate(state.source_document)
        options = FixtureOptions.model_validate(state.fixture_options)
        manifest = VisualManifest.model_validate(state.visual_manifest)
        coverage, reason = calculate_input_coverage(
            str(state.extracted_content.get("content_markdown", "")),
            manifest,
            attachments=state.attachments,
            attachment_content_required=options.attachment_content_required,
            max_text_chars=self.settings.max_structured_text_chars,
        )
        return {
            "input_coverage": coverage,
            "technical_incomplete_reason": reason or state.technical_incomplete_reason,
        }

    def route_review_mode(self, state: ReviewGraphState) -> dict[str, Any]: #a分支：判断是初审还是复审
        previous = PreviousReview.model_validate(state.previous_review) if state.previous_review else None
        return {"review_mode": self.mode_policy.decide(previous)}

    def retrieve_similar_documents(self, state: ReviewGraphState) -> dict[str, Any]:#筛选相似文章
        source = SourceDocument.model_validate(state.source_document)
        selected, audit = self.similarity.retrieve(
            current_document_id=source.document_id,
            current_profile=SimilarityProfile.model_validate(state.similarity_profile),
        )
        return {
            "similarity_retrieval": audit.model_dump(mode="json"),
            "similarity_context": {
                "threshold": self.settings.similarity_threshold,
                "top_k": self.settings.similarity_top_k,
                "effective_candidates": [item.model_dump(mode="json") for item in selected],
            }
        }

    def load_previous_issues(self, state: ReviewGraphState) -> dict[str, Any]: #a分支：读取上一轮结果
        previous = PreviousReview.model_validate(state.previous_review) if state.previous_review else None
        return {
            "similarity_retrieval": SimilarityRetrievalAudit(
                query_document_id=SourceDocument.model_validate(state.source_document).document_id,
                threshold=self.settings.similarity_threshold,
                top_k=self.settings.similarity_top_k,
                skipped_reason="rereview_does_not_recall_candidates",
            ).model_dump(mode="json"),
            "rereview_context": {
                "previous_review_round": previous.review_round if previous else None,
                "blocking_major_issues": self.history_policy.blocking_context(previous),
            }
        }

    def build_multimodal_request(self, state: ReviewGraphState) -> dict[str, Any]:#下一步，组合模型请求
        source = SourceDocument.model_validate(state.source_document).model_copy(
            update={"review_round": state.current_review_round - 1}
        )
        manifest = VisualManifest.model_validate(state.visual_manifest)
        coverage = InputCoverage.model_validate(state.input_coverage)
        pages = [item.model_dump(mode="json") for item in manifest.rendered_pages]#页面png
        prompt = self.prompt_builder.build(                 #prompt组合
            source=source,
            content=str(state.extracted_content.get("content_markdown", "")),
            manifest=manifest,
            coverage=coverage,
            review_mode=state.review_mode,
            similarity_context=state.similarity_context,
            rereview_context=state.rereview_context,
            attachments=state.attachments,
        )
        return {"prompt": prompt, "selected_pages": pages}

    def invoke_kimi(self, state: ReviewGraphState) -> dict[str, Any]:     #决定是否调用模型
        if state.technical_incomplete_reason:
            payload = incomplete_payload(state.technical_incomplete_reason)
            return {
                "raw_model_output": {
                    "content": payload.model_dump_json(),
                    "source": "deterministic_input_guard",
                    "finish_reason": "not_called",
                },
                "visual_evidence": payload.visual_evidence_assessment.model_dump(mode="json"),
            }

        pages = list(state.selected_pages)
        prompt = state.prompt
        calls = list(state.model_calls)
        summaries = list(state.request_summaries)
        evidence_batches: list[dict[str, Any]] = []

        if len(pages) > self.settings.direct_page_limit:                #长pdf判断
            for offset in range(0, len(pages), self.settings.vision_batch_size):
                batch = pages[offset : offset + self.settings.vision_batch_size]
                request = ModelRequest(
                    phase="visual_batch",
                    prompt=self.prompt_builder.build_visual_batch(batch),
                    pages=batch,
                    schema_name="visual_evidence_batch",
                    json_schema=VisualEvidenceBatch.model_json_schema(),
                )
                summaries.append(request_summary(request))
                response = self.model.invoke(request)
                if response.record:
                    calls.append(response.record.model_dump(mode="json"))
                try:
                    evidence_batches.append(VisualEvidenceBatch.model_validate_json(response.content).model_dump(mode="json"))
                except ValidationError as exc:
                    raise ModelSchemaError("长 PDF 视觉证据批次不符合 Schema") from exc
            evidence_pages = [
                evidence["page"]
                for batch in evidence_batches
                for evidence in batch.get("evidence", [])
            ]
            selected_numbers: list[int] = []
            for page_no in [pages[0]["page"], *evidence_pages]:
                if page_no not in selected_numbers:
                    selected_numbers.append(page_no)
                if len(selected_numbers) >= self.settings.final_key_page_limit:
                    break
            pages = [page for page in pages if page["page"] in selected_numbers]
            source = SourceDocument.model_validate(state.source_document)
            manifest = VisualManifest.model_validate(state.visual_manifest)
            coverage = InputCoverage.model_validate(state.input_coverage)
            prompt = self.prompt_builder.build(
                source=source,
                content=str(state.extracted_content.get("content_markdown", "")),
                manifest=manifest,
                coverage=coverage,
                review_mode=state.review_mode,
                similarity_context=state.similarity_context,
                rereview_context=state.rereview_context,
                attachments=state.attachments,
                visual_evidence={"batches": evidence_batches},
            )

        request = ModelRequest(            #请求
            phase="final_review",
            prompt=prompt,
            pages=pages,
            schema_name="review_result",
            json_schema=ModelReviewPayload.model_json_schema(),
        )
        summaries.append(request_summary(request))
        response = self.model.invoke(request)
        if response.record:
            calls.append(response.record.model_dump(mode="json"))
        return {
            "prompt": prompt,
            "selected_pages": pages,
            "visual_evidence": {"schema_version": "2.0", "batches": evidence_batches},
            "raw_model_output": {
                "content": response.content,
                "source": state.model_mode,
                "finish_reason": response.finish_reason,
                "token_usage": response.token_usage,
            },
            "model_calls": calls,
            "request_summaries": summaries,
        }

    def parse_result(self, state: ReviewGraphState) -> dict[str, Any]:#解析模型返回的 JSON
        raw = str(state.raw_model_output.get("content", ""))
        last_error: ReviewError | None = None
        calls = list(state.model_calls)
        summaries = list(state.request_summaries)
        for retry in range(self.settings.kimi_schema_retry_attempts + 1):
            try:
                if retry == 0 and state.raw_model_output.get("finish_reason") == "length":
                    raise ModelJsonError("模型输出因长度限制被截断")
                parsed = self.parser.parse(raw)
                return {
                    "parsed_review_result": parsed.model_dump(mode="json"),
                    "model_calls": calls,
                    "request_summaries": summaries,
                }
            except (ModelJsonError, ModelSchemaError) as exc:
                last_error = exc
                if retry >= self.settings.kimi_schema_retry_attempts or state.raw_model_output.get("source") == "deterministic_input_guard":
                    raise
                repair = ModelRequest(
                    phase="schema_repair",
                    prompt=state.prompt + "\n\n上一次响应不符合 JSON Schema。请重新输出完整 JSON，不要添加 Markdown 围栏。",
                    pages=state.selected_pages,
                    schema_name="review_result",
                    json_schema=ModelReviewPayload.model_json_schema(),
                )
                summaries.append(request_summary(repair))
                response = self.model.invoke(repair)
                if response.record:
                    calls.append(response.record.model_dump(mode="json"))
                raw = response.content
        raise last_error or ModelSchemaError("模型结果解析失败")

    def normalize_result(self, state: ReviewGraphState) -> dict[str, Any]:   #结果修正
        payload = ModelReviewPayload.model_validate(state.parsed_review_result)
        coverage = InputCoverage.model_validate(state.input_coverage)
        result = self.normalizer.normalize(
            payload,
            coverage,
            review_mode=state.review_mode,
            effective_candidate_count=len(state.similarity_context.get("effective_candidates", []))
            if state.review_mode == "initial"
            else None,
        )
        return {"parsed_review_result": result.model_dump(mode="json")}

    def validate_result(self, state: ReviewGraphState) -> dict[str, Any]: #一致性检测
        result = ReviewResult.model_validate(state.parsed_review_result)
        required_issue_ids = None
        if state.review_mode == "rereview":
            required_issue_ids = [
                str(issue["issue_id"])
                for issue in state.rereview_context.get("blocking_major_issues", [])
            ]
        self.validator.validate(result, required_rereview_issue_ids=required_issue_ids)
        return {}

    def project_status(self, state: ReviewGraphState) -> dict[str, Any]:#审稿结论映射为业务状态
        result = ReviewResult.model_validate(state.parsed_review_result)
        status = self.mapper.map(result.result)
        result.local_status = status
        return {
            "projected_status": status,
            "parsed_review_result": result.model_dump(mode="json"),
            "review_completed_at": state.review_completed_at or datetime.now(UTC).isoformat(),
        }

    def render_notifications(self, state: ReviewGraphState) -> dict[str, Any]: #生成投稿人和管理员通知
        source = SourceDocument.model_validate(state.source_document)
        result = ReviewResult.model_validate(state.parsed_review_result)
        submitter, admin = self.notifications.render(
            source, result, review_round=state.current_review_round
        )
        return {"submitter_notification": submitter, "admin_notification": admin}

    def save_artifacts(self, state: ReviewGraphState) -> dict[str, Any]:
        output = Path(state.output_dir)
        result = ReviewResult.model_validate(state.parsed_review_result)
        source = SourceDocument.model_validate(state.source_document)
        interim_trace = _trace(state)
        documents: dict[str, Any] = {
            "source_document.json": state.source_document,
            "extracted_content.json": state.extracted_content,
            "visual_manifest.json": state.visual_manifest,
            "visual_evidence.json": state.visual_evidence,
            "input_coverage.json": state.input_coverage,
            "similarity_profile.json": state.similarity_profile,
            "similarity_retrieval.json": state.similarity_retrieval,
            "review_history_lookup.json": state.review_history_lookup,
            "model_request_summary.json": {
                "model": self.settings.kimi_model if state.model_mode == "real" else "fake-kimi",
                "calls": state.model_calls,
                "requests": state.request_summaries,
            },
            "raw_model_output.json": state.raw_model_output,
            "parsed_review_result.json": state.parsed_review_result,
            "review_history.json": {
                "document_id": source.document_id,
                "previous_review": state.previous_history_record,
                "current_review": ReviewHistoryRecord(
                    run_id=state.run_id,
                    review_round=state.current_review_round,
                    completed_at=state.review_completed_at,
                    result=result,
                ).model_dump(mode="json"),
            },
        }
        for name, payload in documents.items():
            atomic_write_json(output / name, payload, allow_identical=True)
        atomic_write_text(output / "prompt.txt", state.prompt, allow_identical=True)
        atomic_write_text(output / "submitter_notification.txt", state.submitter_notification, allow_identical=True)
        atomic_write_text(output / "admin_notification.txt", state.admin_notification, allow_identical=True)
        atomic_replace_json(output / "run_trace.json", {"events": interim_trace, "result": result.result.value})
        return {}

    def persist_review_history(self, state: ReviewGraphState) -> dict[str, Any]:
        source = SourceDocument.model_validate(state.source_document)
        record = ReviewHistoryRecord(
            run_id=state.run_id,
            review_round=state.current_review_round,
            completed_at=state.review_completed_at,
            result=ReviewResult.model_validate(state.parsed_review_result),
        )
        self.history.append(source.document_id, record)
        return {"review_history_persisted": True}

    def persist_similarity_profile(self, state: ReviewGraphState) -> dict[str, Any]:
        output = Path(state.output_dir)
        source = SourceDocument.model_validate(state.source_document)
        result = ReviewResult.model_validate(state.parsed_review_result)
        coverage = InputCoverage.model_validate(state.input_coverage)
        profile = SimilarityProfile.model_validate(state.similarity_profile)
        eligible = (
            result.result.value == "pass"
            and profile.source in {"blocks", "pdf"}
            and bool(profile.content.strip())
            and not coverage.input_truncated
            and not coverage.missing_sources
        )
        if eligible:
            self.similarity_index.upsert(
                source=source,
                profile=profile,
                review_result=result.result,
                local_status=result.local_status,
            )
        final_trace = _trace(state) + [
            _event("persist_similarity_profile", "completed", persisted=eligible)
        ]
        atomic_replace_json(
            output / "run_trace.json", {"events": final_trace, "result": result.result.value}
        )
        return {"similarity_profile_persisted": eligible}

    def record_safe_failure(self, state: ReviewGraphState) -> dict[str, Any]:
        failure = state.failure or {
            "type": "UnknownFailure",
            "code": "unknown_failure",
            "message": "运行失败。",
            "retryable": False,
        }
        trace = _trace(state) + [_event("record_safe_failure", "completed", error=failure)]
        self.audit.save_failure(Path(state.output_dir), failure, trace)
        return {"trace": trace}
