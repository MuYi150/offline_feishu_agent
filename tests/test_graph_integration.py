from __future__ import annotations

import json
from pathlib import Path

import pytest

from wiki_review_v2.errors import OutputConflictError, SimilarityIndexError
from wiki_review_v2.fixtures import FixtureDocumentSource
from wiki_review_v2.graph import ReviewWorkflow
from wiki_review_v2.model import ModelResponse
from wiki_review_v2.runner import ReviewRunner
from wiki_review_v2.similarity_index import SimilarityIndexStore
from wiki_review_v2.storage import ReviewHistoryStore


@pytest.mark.parametrize(
    ("case_id", "expected"),
    [
        ("basic_pass", "pass"),
        ("need_revision", "need_revision"),
        ("reject_ai_generated", "reject"),
        ("human_review", "recommend_human_review"),
        ("partial_pages", "incomplete_review"),
        ("similarity_merge", "pass"),
        ("rereview_resolved", "pass"),
        ("multimodal_pass", "pass"),
        ("drone_hardware_rd", "pass"),
        ("drone_hardware_rd_round2_pass", "pass"),
        ("drone_hardware_rd_similarity_candidate", "pass"),
    ],
)
def test_fake_graph_outcomes(settings, case_id: str, expected: str) -> None:
    summary = ReviewRunner(settings).run_case(case_id)
    assert summary.ok
    assert summary.result == expected
    parsed = json.loads((summary.output_dir / "parsed_review_result.json").read_text(encoding="utf-8"))
    assert parsed["result"] == expected
    for name in (
        "source_document.json",
        "extracted_content.json",
        "document_extraction.json",
        "visual_selection.json",
        "visual_manifest.json",
        "visual_evidence.json",
        "input_coverage.json",
        "similarity_profile.json",
        "similarity_retrieval.json",
        "article_overview.json",
        "retrieval_article_overview.json",
        "review_history_lookup.json",
        "prompt.txt",
        "model_request_summary.json",
        "raw_model_output.json",
        "parsed_review_result.json",
        "submitter_notification.txt",
        "admin_notification.txt",
        "run_trace.json",
        "checkpoint.sqlite",
    ):
        assert (summary.output_dir / name).exists(), name


def test_invalid_json_and_api_failure_have_no_success_artifacts(settings) -> None:
    for case_id, code in (("invalid_model_output", "model_non_json"), ("api_failure", "model_call_error")):
        summary = ReviewRunner(settings).run_case(case_id)
        assert not summary.ok
        assert summary.failure["code"] == code
        assert not (summary.output_dir / "parsed_review_result.json").exists()
        assert not (summary.output_dir / "submitter_notification.txt").exists()
        assert (summary.output_dir / "failure.json").exists()


def test_source_document_artifact_keeps_v1_field_order(settings) -> None:
    summary = ReviewRunner(settings).run_case("basic_pass")
    source = json.loads(
        (summary.output_dir / "source_document.json").read_text(encoding="utf-8")
    )
    assert list(source) == [
        "case_id",
        "document_id",
        "node_token",
        "title",
        "wiki_name",
        "author_id",
        "author",
        "link",
        "review_method",
        "status",
        "review_round",
        "updated_at",
        "last_ai_review_at",
        "previous_issues",
        "schema_version",
    ]


def test_long_pdf_uses_native_text_and_one_final_review(settings) -> None:
    summary = ReviewRunner(settings).run_case("long_pdf")
    assert summary.ok
    request = json.loads((summary.output_dir / "model_request_summary.json").read_text(encoding="utf-8"))
    assert [call["phase"] for call in request["calls"]] == [
        "retrieval_overview",
        "final_review",
    ]
    assert request["requests"][0]["image_count"] == 0
    assert request["requests"][-1]["image_count"] == 0
    assert json.loads((summary.output_dir / "visual_manifest.json").read_text(encoding="utf-8"))["total_pages"] == 16
    selection = json.loads(
        (summary.output_dir / "visual_selection.json").read_text(encoding="utf-8")
    )
    extraction = json.loads(
        (summary.output_dir / "document_extraction.json").read_text(encoding="utf-8")
    )
    assert selection["mode"] == "selective_regions"
    assert extraction["text_pages_covered"] == 16
    assert extraction["structured_content_source"] == "pdf"
    prompt = (summary.output_dir / "prompt.txt").read_text(encoding="utf-8")
    assert "[PDF第1页]" in prompt and "[PDF第16页]" in prompt
    assert "## BatchedVisualEvidence" not in prompt
    assert "## OutputRequirements" in prompt


def test_long_mixed_pdf_selects_regions_without_visual_batch(settings) -> None:
    summary = ReviewRunner(settings).run_case("long_pdf_mixed")
    assert summary.ok and summary.result == "pass"
    request = json.loads(
        (summary.output_dir / "model_request_summary.json").read_text(encoding="utf-8")
    )
    selection = json.loads(
        (summary.output_dir / "visual_selection.json").read_text(encoding="utf-8")
    )
    extraction = json.loads(
        (summary.output_dir / "document_extraction.json").read_text(encoding="utf-8")
    )
    assert [call["phase"] for call in request["calls"]] == [
        "retrieval_overview",
        "final_review",
    ]
    assert request["requests"][0]["image_count"] == 0
    assert 0 < request["requests"][1]["image_count"] < 14
    assert all(image["type"] for image in request["requests"][1]["images"])
    assert {item["type"] for item in selection["selected"]} >= {
        "embedded_image",
        "table_crop",
        "diagram_crop",
        "scanned_page_fallback",
    }
    assert any(page["simple_table_count"] for page in extraction["pages"])
    assert any(item["reason"] == "duplicate_sha256" for item in selection["decisions"])


def test_fixture_review_round_without_local_history_remains_initial(settings) -> None:
    summary = ReviewRunner(settings).run_case("rereview_resolved")
    prompt = (summary.output_dir / "prompt.txt").read_text(encoding="utf-8")
    lookup = json.loads(
        (summary.output_dir / "review_history_lookup.json").read_text(encoding="utf-8")
    )
    assert lookup["history_found"] is False
    assert "previous-1" not in prompt
    assert "## InitialReviewSimilarityGuide" in prompt
    assert "## ReReviewGuide" not in prompt


def test_drone_round2_uses_real_round1_major_issue_ids(settings) -> None:
    runner = ReviewRunner(settings)
    first = runner.run_case("drone_hardware_rd_round1_need_revision", run_id="drone-round1")
    assert first.ok and first.result == "need_revision"
    summary = runner.run_case("drone_hardware_rd_round2_pass", run_id="drone-round2")
    assert summary.ok
    assert summary.result == "pass"
    prompt = (summary.output_dir / "prompt.txt").read_text(encoding="utf-8")
    result = json.loads((summary.output_dir / "parsed_review_result.json").read_text(encoding="utf-8"))
    lookup = json.loads(
        (summary.output_dir / "review_history_lookup.json").read_text(encoding="utf-8")
    )
    history = json.loads(
        (settings.state_root / "review_history" / "test_doc_drone_hardware_rd.json").read_text(
            encoding="utf-8"
        )
    )

    expected_ids = {
        "drone-major-mcu-selection",
        "drone-major-power-design",
        "drone-major-verification-criteria",
    }
    assert all(issue_id in prompt for issue_id in expected_ids)
    first_result = json.loads(
        (first.output_dir / "parsed_review_result.json").read_text(encoding="utf-8")
    )
    expected_issues = [
        issue for issue in first_result["issues"] if issue["level"] in {"blocking", "major"}
    ]
    history_context_text = prompt.split("## ReReviewHistoryContext\n", 1)[1].split(
        "\n\n## DecisionRules", 1
    )[0]
    history_context = json.loads(history_context_text)
    assert history_context["blocking_major_issues"] == expected_issues
    for issue in expected_issues:
        assert issue["problem"] in prompt
        assert issue["suggestion"] in prompt
        assert issue["position"] in prompt
        assert all(evidence_id in prompt for evidence_id in issue["evidence_ids"])
    assert "## ReReviewGuide" in prompt
    assert result["similarity_check"]["status"] == "not_applicable"
    assert {item["issue_id"] for item in result["re_review_assessment"]["resolutions"]} == expected_ids
    assert lookup == {
        "schema_version": "2.0",
        "document_id": "test_doc_drone_hardware_rd",
        "lookup_status": "ok",
        "history_found": True,
        "source": "local_history",
        "selected_run_id": "drone-round1",
        "selected_review_round": 1,
        "selected_result": "need_revision",
        "total_history_records": 1,
        "blocking_major_issue_count": 3,
        "error_code": None,
    }
    assert [(item["run_id"], item["review_round"]) for item in history["records"]] == [
        ("drone-round1", 1),
        ("drone-round2", 2),
    ]
    overview_audit = json.loads(
        (summary.output_dir / "retrieval_article_overview.json").read_text(encoding="utf-8")
    )
    indexed = SimilarityIndexStore(settings.similarity_index_path).query(
        exclude_document_id="another-document"
    )
    assert overview_audit["persisted_to_index"] is True
    assert indexed[0].content == overview_audit["overview"]["content"]


def test_outputs_do_not_contain_base64_or_authorization(settings) -> None:
    summary = ReviewRunner(settings).run_case("multimodal_pass")
    for path in summary.output_dir.glob("*"):
        if path.is_file() and path.suffix in {".json", ".txt"}:
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert ";base64," not in text
            assert "Authorization" not in text


def test_existing_output_is_not_overwritten(settings) -> None:
    runner = ReviewRunner(settings)
    runner.run_case("basic_pass", run_id="fixed")
    with pytest.raises(OutputConflictError):
        runner.run_case("basic_pass", run_id="fixed")


def test_checkpoint_resume_replays_only_failed_save_node(settings, monkeypatch) -> None:
    original = ReviewWorkflow.save_artifacts
    called = {"count": 0}

    def fail_once(self, state):
        called["count"] += 1
        if called["count"] == 1:
            raise RuntimeError("injected save interruption")
        return original(self, state)

    monkeypatch.setattr(ReviewWorkflow, "save_artifacts", fail_once)
    runner = ReviewRunner(settings)
    first = runner.run_case("basic_pass", run_id="resume-case")
    assert not first.ok
    resumed = runner.resume(first.output_dir)
    assert resumed.ok
    request = json.loads((resumed.output_dir / "model_request_summary.json").read_text(encoding="utf-8"))
    assert [call["phase"] for call in request["calls"]] == [
        "retrieval_overview",
        "final_review",
    ]
    history = ReviewHistoryStore(settings.state_root)
    assert history.record_count("test_doc_basic_pass") == 1


def test_legacy_fixture_candidates_are_ignored(settings) -> None:
    summary = ReviewRunner(settings).run_case("similarity_merge")
    assert summary.ok and summary.result == "pass"
    audit = json.loads((summary.output_dir / "similarity_retrieval.json").read_text(encoding="utf-8"))
    prompt = (summary.output_dir / "prompt.txt").read_text(encoding="utf-8")
    assert audit["index_candidate_count"] == 0
    assert audit["prompt_candidates"] == []
    assert "existing-1" not in prompt


def test_two_drone_fixtures_recall_from_local_index_only(settings) -> None:
    runner = ReviewRunner(settings)
    source_run = runner.run_case("similarity_drone_source", run_id="source")
    assert source_run.ok and source_run.result == "pass"
    source_profile = json.loads(
        (source_run.output_dir / "similarity_profile.json").read_text(encoding="utf-8")
    )
    source_result = json.loads(
        (source_run.output_dir / "parsed_review_result.json").read_text(encoding="utf-8")
    )
    source_overview_audit = json.loads(
        (source_run.output_dir / "retrieval_article_overview.json").read_text(encoding="utf-8")
    )
    source_audit = json.loads(
        (source_run.output_dir / "similarity_retrieval.json").read_text(encoding="utf-8")
    )
    assert source_profile["source"] == "blocks"
    assert source_profile["query_text"] != source_overview_audit["overview"]["content"]
    assert source_overview_audit["persisted_to_index"] is True
    assert source_audit["prompt_candidates"] == []
    assert SimilarityIndexStore(settings.similarity_index_path).info()["record_count"] == 1
    source_requests = json.loads(
        (source_run.output_dir / "model_request_summary.json").read_text(encoding="utf-8")
    )
    assert [call["phase"] for call in source_requests["calls"]] == [
        "retrieval_overview",
        "final_review",
    ]

    candidate_run = runner.run_case("similarity_drone_candidate", run_id="candidate")
    assert candidate_run.ok and candidate_run.result == "pass"
    audit = json.loads(
        (candidate_run.output_dir / "similarity_retrieval.json").read_text(encoding="utf-8")
    )
    prompt = (candidate_run.output_dir / "prompt.txt").read_text(encoding="utf-8")
    assert audit["index_candidate_count"] == 1
    assert len(audit["prompt_candidates"]) == 1
    recalled = audit["prompt_candidates"][0]
    assert recalled["document_id"] == "test_doc_similarity_drone_source"
    assert recalled["similarity_score"] >= settings.similarity_threshold
    assert recalled["content"] == source_overview_audit["overview"]["content"]
    assert recalled["summary_source"] == "ai_retrieval_overview"
    assert recalled["overview_model"] == "fake-kimi"
    assert recalled["overview_prompt_version"] == "retrieval_overview_v1"
    assert recalled["score_details"]["final_score"] == recalled["similarity_score"]
    assert "test_doc_similarity_drone_source" in prompt
    assert "设计_四旋翼飞控硬件平台研发方案-v1.0" in prompt
    context_text = prompt.split("## InitialReviewSimilarityContext\n", 1)[1].split(
        "\n\n## DecisionRules", 1
    )[0]
    context = json.loads(context_text)
    assert context["effective_candidates"][0]["content"] == source_overview_audit["overview"]["content"]
    assert f'"similarity_score": {recalled["similarity_score"]}' in prompt
    assert "source.pdf" not in prompt and "page-0001.png" not in prompt
    assert SimilarityIndexStore(settings.similarity_index_path).info()["record_count"] == 2


def test_non_pass_and_model_failure_do_not_enter_index(settings) -> None:
    runner = ReviewRunner(settings)
    revision = runner.run_case("need_revision")
    failure = runner.run_case("api_failure")
    assert revision.ok and revision.result == "need_revision"
    assert not failure.ok
    assert SimilarityIndexStore(settings.similarity_index_path).info()["record_count"] == 0


def test_invalid_retrieval_overview_stops_before_final_review(settings) -> None:
    class InvalidOverviewModel:
        def __init__(self) -> None:
            self.phases: list[str] = []

        def invoke(self, request):
            self.phases.append(request.phase)
            assert request.pages == []
            return ModelResponse(content="not-json")

    model = InvalidOverviewModel()
    summary = ReviewRunner(settings).run_case(
        "basic_pass", run_id="invalid-retrieval-overview", model_override=model
    )
    assert not summary.ok
    assert summary.failure["code"] == "model_non_json"
    assert model.phases == ["retrieval_overview", "retrieval_overview_schema_repair"]
    assert "final_review" not in model.phases
    assert SimilarityIndexStore(settings.similarity_index_path).info()["record_count"] == 0


def test_retrieval_overview_schema_repair_precedes_final_review(settings) -> None:
    fixture = FixtureDocumentSource().load(settings.fixtures_root / "basic_pass")

    class RepairOverviewModel:
        def __init__(self) -> None:
            self.phases: list[str] = []

        def invoke(self, request):
            self.phases.append(request.phase)
            if request.phase == "retrieval_overview":
                return ModelResponse(content="{}")
            if request.phase == "retrieval_overview_schema_repair":
                return ModelResponse(
                    content=json.dumps(fixture.fake_model_response["overview"], ensure_ascii=False)
                )
            return ModelResponse(
                content=json.dumps(fixture.fake_model_response["review"], ensure_ascii=False)
            )

    model = RepairOverviewModel()
    summary = ReviewRunner(settings).run_case(
        "basic_pass", run_id="repair-retrieval-overview", model_override=model
    )
    assert summary.ok
    assert model.phases == [
        "retrieval_overview",
        "retrieval_overview_schema_repair",
        "final_review",
    ]


def test_null_article_overview_uses_existing_schema_repair(settings) -> None:
    valid = FixtureDocumentSource().load(
        settings.fixtures_root / "basic_pass"
    ).fake_model_response["review"]
    overview = FixtureDocumentSource().load(
        settings.fixtures_root / "basic_pass"
    ).fake_model_response["overview"]
    invalid = json.loads(json.dumps(valid, ensure_ascii=False))
    invalid["article_overview"] = None

    class SequenceModel:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, request):
            self.calls += 1
            if request.phase.startswith("retrieval_overview"):
                return ModelResponse(content=json.dumps(overview, ensure_ascii=False))
            payload = invalid if self.calls == 1 else valid
            payload = invalid if request.phase == "final_review" else valid
            return ModelResponse(content=json.dumps(payload, ensure_ascii=False))

    model = SequenceModel()
    summary = ReviewRunner(settings).run_case(
        "basic_pass", run_id="overview-repair", model_override=model
    )
    assert summary.ok and model.calls == 3
    requests = json.loads(
        (summary.output_dir / "model_request_summary.json").read_text(encoding="utf-8")
    )
    assert [item["phase"] for item in requests["requests"]] == [
        "retrieval_overview",
        "final_review",
        "schema_repair",
    ]


def test_pass_with_ungrounded_but_present_article_overview_is_indexed(settings) -> None:
    payload = FixtureDocumentSource().load(
        settings.fixtures_root / "basic_pass"
    ).fake_model_response["review"]
    payload = json.loads(json.dumps(payload, ensure_ascii=False))
    payload["article_overview"]["technical_entities"] = ["UNSUPPORTED-CHIP-999"]
    overview = FixtureDocumentSource().load(
        settings.fixtures_root / "basic_pass"
    ).fake_model_response["overview"]

    class StaticModel:
        def invoke(self, request):
            response = overview if request.phase.startswith("retrieval_overview") else payload
            return ModelResponse(content=json.dumps(response, ensure_ascii=False))

    summary = ReviewRunner(settings).run_case(
        "basic_pass", run_id="overview-invalid", model_override=StaticModel()
    )
    assert summary.ok and summary.result == "pass"
    audit = json.loads(
        (summary.output_dir / "article_overview.json").read_text(encoding="utf-8")
    )
    assert audit["validation_status"] == "invalid"
    assert audit["persisted_to_index"] is False
    retrieval_audit = json.loads(
        (summary.output_dir / "retrieval_article_overview.json").read_text(encoding="utf-8")
    )
    assert retrieval_audit["persisted_to_index"] is True
    indexed = SimilarityIndexStore(settings.similarity_index_path).query(
        exclude_document_id="another-document"
    )
    assert len(indexed) == 1
    assert indexed[0].content == overview["content"]
    assert indexed[0].technical_entities == overview["technical_entities"]


def test_checkpoint_resume_retries_only_similarity_upsert(settings, monkeypatch) -> None:
    original = SimilarityIndexStore.upsert
    calls = {"count": 0}

    def fail_once(self, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise SimilarityIndexError("injected index failure")
        return original(self, **kwargs)

    monkeypatch.setattr(SimilarityIndexStore, "upsert", fail_once)
    runner = ReviewRunner(settings)
    first = runner.run_case("basic_pass", run_id="resume-similarity-index")
    assert not first.ok
    assert first.failure["code"] == "similarity_index_error"
    assert (first.output_dir / "parsed_review_result.json").exists()
    resumed = runner.resume(first.output_dir)
    assert resumed.ok
    requests = json.loads(
        (resumed.output_dir / "model_request_summary.json").read_text(encoding="utf-8")
    )
    assert [call["phase"] for call in requests["calls"]] == [
        "retrieval_overview",
        "final_review",
    ]
    assert calls["count"] == 2
    assert SimilarityIndexStore(settings.similarity_index_path).info()["record_count"] == 1


def test_checkpoint_resume_after_history_write_does_not_duplicate_record(
    settings, monkeypatch
) -> None:
    original = ReviewHistoryStore.append
    calls = {"count": 0}

    def append_then_fail_once(self, document_id, record):
        calls["count"] += 1
        saved = original(self, document_id, record)
        if calls["count"] == 1:
            raise RuntimeError("injected interruption after history replace")
        return saved

    monkeypatch.setattr(ReviewHistoryStore, "append", append_then_fail_once)
    runner = ReviewRunner(settings)
    first = runner.run_case("basic_pass", run_id="resume-history")
    assert not first.ok
    assert ReviewHistoryStore(settings.state_root).record_count("test_doc_basic_pass") == 1

    resumed = runner.resume(first.output_dir)
    assert resumed.ok
    assert calls["count"] == 2
    assert ReviewHistoryStore(settings.state_root).record_count("test_doc_basic_pass") == 1
    request = json.loads(
        (resumed.output_dir / "model_request_summary.json").read_text(encoding="utf-8")
    )
    assert [call["phase"] for call in request["calls"]] == [
        "retrieval_overview",
        "final_review",
    ]
