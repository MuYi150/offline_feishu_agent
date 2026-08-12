from __future__ import annotations

import json

import pytest

from wiki_review_v2.errors import ReviewHistoryError
from wiki_review_v2.runner import ReviewRunner
from wiki_review_v2.storage import ReviewHistoryStore


def _completed_record(*, run_id: str, review_round: int, result: dict) -> dict:
    return {
        "run_id": run_id,
        "review_round": review_round,
        "completed_at": "2026-08-12T01:00:00+00:00",
        "result": result,
    }


def _result_from_run(settings) -> dict:
    summary = ReviewRunner(settings).run_case("basic_pass", run_id="seed-result")
    return json.loads(
        (summary.output_dir / "parsed_review_result.json").read_text(encoding="utf-8")
    )


def test_missing_history_returns_none_and_zero_count(tmp_path) -> None:
    store = ReviewHistoryStore(tmp_path)
    assert store.load_latest("missing") is None
    assert store.record_count("missing") == 0


def test_legacy_history_without_completed_at_uses_last_array_record(settings, tmp_path) -> None:
    result = _result_from_run(settings)
    root = tmp_path / "legacy-state"
    path = root / "review_history" / "doc.json"
    path.parent.mkdir(parents=True)
    records = [
        {"run_id": "old-1", "review_round": 1, "result": result},
        {"run_id": "old-2", "review_round": 2, "result": result},
    ]
    path.write_text(
        json.dumps({"schema_version": "2.0", "document_id": "doc", "records": records}),
        encoding="utf-8",
    )
    latest = ReviewHistoryStore(root).load_latest("doc")
    assert latest is not None
    assert latest.run_id == "old-2"
    assert latest.review_round == 2
    assert latest.completed_at == ""


def test_append_is_idempotent_by_run_id_and_rejects_conflict(settings, tmp_path) -> None:
    result = _result_from_run(settings)
    store = ReviewHistoryStore(tmp_path / "state")
    record = _completed_record(run_id="same-run", review_round=1, result=result)
    store.append("doc", record)
    store.append("doc", record)
    assert store.record_count("doc") == 1

    conflicting = _completed_record(run_id="same-run", review_round=2, result=result)
    with pytest.raises(ReviewHistoryError):
        store.append("doc", conflicting)
    assert store.record_count("doc") == 1


def test_document_id_mismatch_and_invalid_latest_record_are_errors(tmp_path) -> None:
    root = tmp_path / "state"
    path = root / "review_history" / "doc.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"document_id": "another-doc", "records": []}), encoding="utf-8"
    )
    with pytest.raises(ReviewHistoryError):
        ReviewHistoryStore(root).load_latest("doc")

    with pytest.raises(ReviewHistoryError):
        ReviewHistoryStore(root).append("another", {"result": "pass"})

    path.write_text(
        json.dumps({"document_id": "doc", "records": [{"run_id": "broken"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ReviewHistoryError):
        ReviewHistoryStore(root).load_latest("doc")


def test_different_document_id_never_reuses_history(settings) -> None:
    runner = ReviewRunner(settings)
    first = runner.run_case("basic_pass", run_id="document-a")
    second = runner.run_case("need_revision", run_id="document-b")
    first_lookup = json.loads(
        (first.output_dir / "review_history_lookup.json").read_text(encoding="utf-8")
    )
    second_lookup = json.loads(
        (second.output_dir / "review_history_lookup.json").read_text(encoding="utf-8")
    )
    assert first_lookup["history_found"] is False
    assert second_lookup["history_found"] is False
