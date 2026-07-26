from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from wiki_review_v2.errors import FixtureError, ModelAuthenticationError
from wiki_review_v2.fixtures import FixtureDocumentSource
from wiki_review_v2.model import KimiMultimodalModel, ModelRequest
from wiki_review_v2.runner import ReviewRunner
from wiki_review_v2.storage import ReviewHistoryStore


def clone_case(settings, tmp_path: Path, case_id: str = "multimodal_pass") -> tuple[object, Path]:
    fixtures = tmp_path / "fixtures"
    source = settings.project_root / "fixtures" / case_id
    target = fixtures / case_id
    shutil.copytree(source, target)
    updated = settings.model_copy(update={"fixtures_root": fixtures})
    return updated, target


def test_missing_and_corrupt_pdf_become_deterministic_incomplete(settings, tmp_path: Path) -> None:
    missing_settings, missing = clone_case(settings, tmp_path / "missing")
    (missing / "source.pdf").unlink()
    first = ReviewRunner(missing_settings).run_case("multimodal_pass")
    assert first.result == "incomplete_review"
    assert json.loads((first.output_dir / "raw_model_output.json").read_text(encoding="utf-8"))["source"] == "deterministic_input_guard"

    corrupt_settings, corrupt = clone_case(settings, tmp_path / "corrupt")
    (corrupt / "source.pdf").write_bytes(b"not-a-pdf")
    second = ReviewRunner(corrupt_settings).run_case("multimodal_pass")
    assert second.result == "incomplete_review"


def test_page_count_pdf_size_and_text_limits_are_explicit(settings, tmp_path: Path) -> None:
    page_settings = settings.model_copy(update={"max_pdf_pages": 1})
    page_result = ReviewRunner(page_settings).run_case("multimodal_pass")
    assert page_result.result == "incomplete_review"
    page_coverage = json.loads((page_result.output_dir / "input_coverage.json").read_text(encoding="utf-8"))
    assert page_coverage["limitations"]

    size_settings = settings.model_copy(update={"max_pdf_bytes": 1})
    size_result = ReviewRunner(size_settings).run_case("multimodal_pass")
    assert size_result.result == "incomplete_review"

    text_settings, case = clone_case(settings, tmp_path / "text", "basic_pass")
    blocks = json.loads((case / "document_blocks.json").read_text(encoding="utf-8"))
    blocks.append({"type": "paragraph", "text": "x" * 2000})
    (case / "document_blocks.json").write_text(json.dumps(blocks, ensure_ascii=False), encoding="utf-8")
    text_settings = text_settings.model_copy(update={"max_structured_text_chars": 1000})
    text_result = ReviewRunner(text_settings).run_case("basic_pass")
    assert text_result.result == "incomplete_review"
    assert json.loads((text_result.output_dir / "input_coverage.json").read_text(encoding="utf-8"))["input_truncated"] is True


def test_empty_model_content_and_bad_fixture_fail_without_notification(settings, tmp_path: Path) -> None:
    empty_settings, case = clone_case(settings, tmp_path / "empty", "basic_pass")
    (case / "fake_model_response.json").write_text('{"raw":""}', encoding="utf-8")
    summary = ReviewRunner(empty_settings).run_case("basic_pass")
    assert summary.failure["code"] == "model_non_json"
    assert not (summary.output_dir / "submitter_notification.txt").exists()

    bad_settings, bad = clone_case(settings, tmp_path / "bad", "basic_pass")
    (bad / "source_document.json").write_text("{bad json", encoding="utf-8")
    with pytest.raises(FixtureError):
        FixtureDocumentSource().load(bad)


def test_empty_structured_text_without_visual_input_is_incomplete(settings, tmp_path: Path) -> None:
    empty_settings, case = clone_case(settings, tmp_path, "basic_pass")
    (case / "document_blocks.json").write_text("[]", encoding="utf-8")
    summary = ReviewRunner(empty_settings).run_case("basic_pass")
    assert summary.result == "incomplete_review"
    coverage = json.loads((summary.output_dir / "input_coverage.json").read_text(encoding="utf-8"))
    assert coverage["structured_text_available"] is False
    assert coverage["visual_pages_complete"] is False


def test_corrupt_history_is_quarantined(tmp_path: Path) -> None:
    store = ReviewHistoryStore(tmp_path)
    history = tmp_path / "review_history" / "doc.json"
    history.parent.mkdir(parents=True)
    history.write_text("not json", encoding="utf-8")
    store.append("doc", {"result": "pass"})
    assert json.loads(history.read_text(encoding="utf-8"))["records"][0]["result"] == "pass"
    assert list(history.parent.glob("doc.corrupt-*.json"))


def test_kimi_retries_rate_limit_and_does_not_retry_authentication(settings) -> None:
    class RateLimitError(Exception):
        pass

    class AuthenticationError(Exception):
        pass

    class ServerError(Exception):
        status_code = 503

    class Completions:
        def __init__(self, failures):
            self.failures = list(failures)
            self.calls = 0

        def create(self, **kwargs):
            self.calls += 1
            if self.failures:
                raise self.failures.pop(0)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}'), finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )

    request = ModelRequest(
        phase="test",
        prompt="test",
        pages=[],
        schema_name="test",
        json_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
    )
    sleeps: list[float] = []
    model = object.__new__(KimiMultimodalModel)
    model.settings = settings.model_copy(update={"kimi_max_attempts": 3})
    model.sleeper = sleeps.append
    rate = Completions([RateLimitError()])
    model.client = SimpleNamespace(chat=SimpleNamespace(completions=rate))
    assert model.invoke(request).content == '{"ok":true}'
    assert rate.calls == 2 and sleeps == [1]

    sleeps.clear()
    server = Completions([ServerError()])
    model.client = SimpleNamespace(chat=SimpleNamespace(completions=server))
    assert model.invoke(request).content == '{"ok":true}'
    assert server.calls == 2 and sleeps == [1]

    auth = Completions([AuthenticationError()])
    model.client = SimpleNamespace(chat=SimpleNamespace(completions=auth))
    with pytest.raises(ModelAuthenticationError):
        model.invoke(request)
    assert auth.calls == 1


def test_real_model_preflight_without_key_writes_safe_failure(settings) -> None:
    settings = settings.model_copy(update={"kimi_api_key": None, "allow_real_model_call": False})
    summary = ReviewRunner(settings).run_case(
        "basic_pass", real_model=True, explicit_real_authorization=True
    )
    assert not summary.ok
    assert summary.failure["code"] == "model_authentication_error"
    assert (summary.output_dir / "failure.json").exists()
    assert not (summary.output_dir / "submitter_notification.txt").exists()
