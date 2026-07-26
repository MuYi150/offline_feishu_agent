from __future__ import annotations

from pathlib import Path

import pytest

from wiki_review_v2.config import Settings


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    base = Settings.from_env(PROJECT_ROOT)
    return base.model_copy(
        update={
            "output_root": tmp_path / "outputs",
            "state_root": tmp_path / "state",
            "kimi_schema_retry_attempts": 1,
        }
    )

