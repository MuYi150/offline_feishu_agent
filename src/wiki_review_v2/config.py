from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_root: Path
    fixtures_root: Path
    output_root: Path
    state_root: Path
    kimi_api_key: SecretStr | None = None
    kimi_base_url: str = "https://api.moonshot.ai/v1"
    kimi_model: str = "kimi-k3"
    allow_real_model_call: bool = False
    kimi_reasoning_effort: str = "low"
    kimi_max_completion_tokens: int = Field(default=8192, ge=256)
    kimi_timeout_seconds: float = Field(default=180.0, gt=0)
    kimi_max_attempts: int = Field(default=3, ge=1, le=10)
    kimi_schema_retry_attempts: int = Field(default=1, ge=0, le=3)
    pdf_render_dpi: int = Field(default=150, ge=72, le=300)
    max_image_long_edge: int = Field(default=2048, ge=512, le=4096)
    direct_page_limit: int = Field(default=12, ge=1)
    direct_image_count_limit: int = Field(default=12, ge=1)
    direct_image_bytes_limit: int = Field(default=15 * 1024 * 1024, ge=1024)
    max_visual_regions: int = Field(default=60, ge=1)
    max_visual_total_bytes: int = Field(default=15 * 1024 * 1024, ge=1024)
    max_full_page_fallbacks: int = Field(default=4, ge=0)
    long_pdf_legacy_batch_fallback: bool = False
    vision_batch_size: int = Field(default=8, ge=1)
    final_key_page_limit: int = Field(default=8, ge=1)
    max_pdf_pages: int = Field(default=100, ge=1)
    max_pdf_bytes: int = Field(default=100 * 1024 * 1024, ge=1)
    max_request_bytes: int = Field(default=90 * 1024 * 1024, ge=1024 * 1024)
    max_structured_text_chars: int = Field(default=500_000, ge=1_000)
    similarity_threshold: float = Field(default=0.35, ge=0, le=1)
    similarity_top_k: int = Field(default=5, ge=1, le=50)
    similarity_overview_max_chars: int = Field(default=1200, ge=200, le=5000)
    similarity_query_max_chars: int = Field(default=30_000, ge=1_000, le=500_000)
    similarity_index_path: Path = Path("local_state/similarity_index/articles.sqlite")

    @field_validator("kimi_reasoning_effort")
    @classmethod
    def validate_effort(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"low", "high", "max"}:
            raise ValueError("KIMI_REASONING_EFFORT must be low, high, or max")
        return value

    @classmethod
    def from_env(cls, project_root: Path | None = None) -> "Settings":
        root = (project_root or Path(__file__).resolve().parents[2]).resolve()
        key = os.getenv("KIMI_API_KEY") or os.getenv("MOONSHOT_API_KEY")
        configured_index = Path(
            os.getenv(
                "SIMILARITY_INDEX_PATH",
                root / "local_state" / "similarity_index" / "articles.sqlite",
            )
        )
        if not configured_index.is_absolute():
            configured_index = root / configured_index
        return cls(
            project_root=root,
            fixtures_root=Path(os.getenv("WIKI_V2_FIXTURES_ROOT", root / "fixtures")),
            output_root=Path(os.getenv("WIKI_V2_OUTPUT_ROOT", root / "outputs")),
            state_root=Path(os.getenv("WIKI_V2_STATE_ROOT", root / "local_state")),
            kimi_api_key=SecretStr(key) if key else None,
            kimi_base_url=os.getenv("KIMI_BASE_URL", "https://api.moonshot.ai/v1"),
            kimi_model=os.getenv("KIMI_MODEL", "kimi-k3"),
            allow_real_model_call=_bool_env("ALLOW_REAL_MODEL_CALL"),
            kimi_reasoning_effort=os.getenv("KIMI_REASONING_EFFORT", "low"),
            kimi_max_completion_tokens=int(os.getenv("KIMI_MAX_COMPLETION_TOKENS", "8192")),
            kimi_timeout_seconds=float(os.getenv("KIMI_TIMEOUT_SECONDS", "180")),
            kimi_max_attempts=int(os.getenv("KIMI_MAX_ATTEMPTS", "3")),
            kimi_schema_retry_attempts=int(os.getenv("KIMI_SCHEMA_RETRY_ATTEMPTS", "1")),
            pdf_render_dpi=int(os.getenv("PDF_RENDER_DPI", "150")),
            max_image_long_edge=int(os.getenv("MAX_IMAGE_LONG_EDGE", "2048")),
            direct_page_limit=int(os.getenv("DIRECT_PAGE_LIMIT", "12")),
            direct_image_count_limit=int(os.getenv("DIRECT_IMAGE_COUNT_LIMIT", "12")),
            direct_image_bytes_limit=int(
                os.getenv("DIRECT_IMAGE_BYTES_LIMIT", str(15 * 1024 * 1024))
            ),
            max_visual_regions=int(os.getenv("MAX_VISUAL_REGIONS", "60")),
            max_visual_total_bytes=int(
                os.getenv("MAX_VISUAL_TOTAL_BYTES", str(15 * 1024 * 1024))
            ),
            max_full_page_fallbacks=int(os.getenv("MAX_FULL_PAGE_FALLBACKS", "4")),
            long_pdf_legacy_batch_fallback=_bool_env("LONG_PDF_LEGACY_BATCH_FALLBACK"),
            vision_batch_size=int(os.getenv("VISION_BATCH_SIZE", "8")),
            final_key_page_limit=int(os.getenv("FINAL_KEY_PAGE_LIMIT", "8")),
            max_pdf_pages=int(os.getenv("MAX_PDF_PAGES", "100")),
            max_pdf_bytes=int(os.getenv("MAX_PDF_BYTES", str(100 * 1024 * 1024))),
            max_request_bytes=int(os.getenv("MAX_REQUEST_BYTES", str(90 * 1024 * 1024))),
            max_structured_text_chars=int(os.getenv("MAX_STRUCTURED_TEXT_CHARS", "500000")),
            similarity_threshold=float(
                os.getenv("SIMILARITY_THRESHOLD", os.getenv("SIMILARITY_MIN_SCORE", "0.35"))
            ),
            similarity_top_k=int(
                os.getenv("SIMILARITY_TOP_K", os.getenv("SIMILARITY_TOP_N", "5"))
            ),
            similarity_overview_max_chars=int(
                os.getenv(
                    "SIMILARITY_OVERVIEW_MAX_CHARS",
                    os.getenv("SIMILARITY_SUMMARY_MAX_CHARS", "1200"),
                )
            ),
            similarity_query_max_chars=int(os.getenv("SIMILARITY_QUERY_MAX_CHARS", "30000")),
            similarity_index_path=configured_index,
        )

    @property
    def similarity_min_score(self) -> float:
        """Compatibility alias for callers using the original setting name."""
        return self.similarity_threshold

    @property
    def similarity_top_n(self) -> int:
        """Compatibility alias for callers using the original setting name."""
        return self.similarity_top_k

    @property
    def similarity_summary_max_chars(self) -> int:
        """Compatibility alias for the old deterministic summary limit."""
        return self.similarity_overview_max_chars

    def safe_dict(self) -> dict[str, object]:
        data = self.model_dump(exclude={"kimi_api_key"}, mode="json")
        data["kimi_api_key_present"] = self.kimi_api_key is not None
        return data
