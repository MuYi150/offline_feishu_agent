from __future__ import annotations

import os

import pytest

from wiki_review_v2.runner import ReviewRunner


def _real_enabled() -> bool:
    key = os.getenv("KIMI_API_KEY") or os.getenv("MOONSHOT_API_KEY")
    return os.getenv("RUN_REAL_KIMI_TESTS") == "1" and bool(key)


@pytest.mark.real_kimi
@pytest.mark.skipif(not _real_enabled(), reason="需要 RUN_REAL_KIMI_TESTS=1 和 Kimi API Key")
@pytest.mark.parametrize("case_id", ["basic_pass", "multimodal_pass"])
def test_real_kimi_smoke(settings, case_id: str) -> None:
    summary = ReviewRunner(settings).run_case(
        case_id,
        real_model=True,
        explicit_real_authorization=True,
    )
    assert summary.ok
    assert (summary.output_dir / "parsed_review_result.json").exists()
    assert (summary.output_dir / "model_request_summary.json").exists()
