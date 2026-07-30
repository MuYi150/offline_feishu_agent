from __future__ import annotations

import json
from pathlib import Path

import pytest

from wiki_review_v2.errors import OutputConflictError
from wiki_review_v2.graph import ReviewWorkflow
from wiki_review_v2.runner import ReviewRunner


@pytest.mark.parametrize(
    ("case_id", "expected"),
    [
        ("basic_pass", "pass"),
        ("need_revision", "need_revision"),
        ("reject_ai_generated", "reject"),
        ("human_review", "recommend_human_review"),
        ("partial_pages", "incomplete_review"),
        ("similarity_merge", "need_revision"),
        ("rereview_resolved", "pass"),
        ("multimodal_pass", "pass"),
        ("drone_hardware_rd", "pass"),
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
        "visual_manifest.json",
        "visual_evidence.json",
        "input_coverage.json",
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


def test_long_pdf_uses_batches_and_key_pages(settings) -> None:
    summary = ReviewRunner(settings).run_case("long_pdf")
    assert summary.ok
    request = json.loads((summary.output_dir / "model_request_summary.json").read_text(encoding="utf-8"))
    assert [call["phase"] for call in request["calls"]] == ["visual_batch", "visual_batch", "final_review"]
    assert request["requests"][-1]["image_count"] <= settings.final_key_page_limit
    assert json.loads((summary.output_dir / "visual_manifest.json").read_text(encoding="utf-8"))["total_pages"] == 16
    prompt = (summary.output_dir / "prompt.txt").read_text(encoding="utf-8")
    assert "## BatchedVisualEvidence" in prompt
    assert "## OutputRequirements" in prompt


def test_rereview_prompt_only_contains_previous_blocking_major(settings) -> None:
    summary = ReviewRunner(settings).run_case("rereview_resolved")
    prompt = (summary.output_dir / "prompt.txt").read_text(encoding="utf-8")
    assert "previous-1" in prompt
    assert "previous-2" not in prompt
    assert "复审不重新召回相似候选" in prompt
    assert "## ReReviewGuide" in prompt
    assert "resolved、partially_resolved 或 unresolved" in prompt
    assert "## InitialReviewSimilarityGuide" not in prompt


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
    assert [call["phase"] for call in request["calls"]] == ["final_review"]
