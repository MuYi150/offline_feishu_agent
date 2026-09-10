from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings


@dataclass(frozen=True)
class FeishuSettings:
    app_id: str = ""
    app_secret: str = field(default="", repr=False)
    domain: str = "my.feishu.cn"
    api_base_url: str = "https://open.feishu.cn/open-apis"
    review_wiki_node_token: str = ""
    config_wiki_node_token: str = ""
    review_sheet_name: str = "文档上传情况"
    config_sheet_name: str = "知识库配置表"
    admin_open_id: str = ""
    enable_admin_notify: bool = True
    http_timeout: float = 30
    http_max_retries: int = 3
    export_timeout: float = 120
    max_review_rounds: int = 3

    def validate(self) -> None:
        for name in ("app_id", "app_secret", "review_wiki_node_token", "config_wiki_node_token"):
            if not getattr(self, name):
                raise ValueError(f"缺少飞书配置：{name}")
        if self.http_timeout <= 0 or self.export_timeout <= 0 or self.http_max_retries < 0 or self.max_review_rounds < 1:
            raise ValueError("超时、重试或最大轮次配置不合法")


ENV_NAMES = {
    "app_id": "FEISHU_APP_ID", "app_secret": "FEISHU_APP_SECRET", "domain": "FEISHU_DOMAIN",
    "api_base_url": "FEISHU_API_BASE_URL", "review_wiki_node_token": "REVIEW_WIKI_NODE_TOKEN",
    "config_wiki_node_token": "CONFIG_WIKI_NODE_TOKEN", "review_sheet_name": "REVIEW_SHEET_NAME",
    "config_sheet_name": "CONFIG_SHEET_NAME", "admin_open_id": "ADMIN_OPEN_ID",
    "enable_admin_notify": "ENABLE_ADMIN_NOTIFY", "http_timeout": "FEISHU_HTTP_TIMEOUT",
    "http_max_retries": "FEISHU_HTTP_MAX_RETRIES", "export_timeout": "PDF_EXPORT_TIMEOUT_SECONDS",
    "max_review_rounds": "MAX_AI_REVIEW_ROUNDS",
}


def load_settings(config_path: Path | None = None) -> tuple[FeishuSettings, Settings, Path]:
    config = json.loads(config_path.read_text(encoding="utf-8-sig")) if config_path else {}
    values = dict(config.get("feishu", {}))
    defaults = FeishuSettings()
    for name, env in ENV_NAMES.items():
        value = os.environ.get(env, values.get(name, getattr(defaults, name)))
        default = getattr(defaults, name)
        if isinstance(default, bool):
            value = str(value).lower() in {"true", "1", "yes", "on"}
        elif isinstance(default, (int, float)):
            value = type(default)(value)
        values[name] = value
    feishu = FeishuSettings(**values)
    # Reuse the existing env parser, but never mutate the process environment.
    model = Settings.from_env()
    model_values = model.model_dump()
    for name, value in config.get("model", {}).items():
        if name not in Settings.model_fields or name.endswith("root") or name == "similarity_index_path":
            raise ValueError(f"不支持的 model 配置字段：{name}")
        env = name.upper()
        aliases = {"kimi_api_key": "MOONSHOT_API_KEY", "similarity_threshold": "SIMILARITY_MIN_SCORE",
                   "similarity_top_k": "SIMILARITY_TOP_N", "similarity_overview_max_chars": "SIMILARITY_SUMMARY_MAX_CHARS"}
        if env not in os.environ and aliases.get(name, "") not in os.environ:
            model_values[name] = value
    model = Settings.model_validate(model_values)
    root = Path(os.environ.get("WIKI_V2_ONLINE_ROOT", config.get("online_root", model.project_root / "online_state")))
    if not root.is_absolute():
        root = (config_path.parent if config_path else model.project_root) / root
    root = root.resolve()
    model = model.model_copy(update={"state_root": root / "history", "output_root": root / "outputs",
                                    "similarity_index_path": root / "published.sqlite"})
    return feishu, model, root
