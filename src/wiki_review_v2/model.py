from __future__ import annotations

import base64
import json
import mimetypes
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .config import Settings
from .errors import (
    ModelAuthenticationError,
    ModelCallError,
    ModelRateLimitError,
    ModelServerError,
    ModelTimeoutError,
)
from .models import ModelCallRecord


@dataclass(frozen=True)
class ModelRequest:
    phase: str
    prompt: str
    pages: list[dict[str, Any]]
    schema_name: str
    json_schema: dict[str, Any]
    system_prompt: str = "你是严格输出结构化 JSON 的科研知识库审稿助手。"
    model: str | None = None


@dataclass(frozen=True)
class ModelResponse:
    content: str
    finish_reason: str = "stop"
    token_usage: dict[str, int] = field(default_factory=dict)
    record: ModelCallRecord | None = None


class ReviewModel(Protocol):
    def invoke(self, request: ModelRequest) -> ModelResponse: ...


class FakeReviewModel:                      #fake模型，直接返回fixture中的内容
    def __init__(self, fixture_response: dict[str, Any]) -> None:
        self.fixture_response = fixture_response
        self.call_count = 0
        self.batch_index = 0

    def invoke(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        overview_phase = request.phase.startswith("retrieval_overview")
        error = (
            self.fixture_response.get("overview_raise")
            if overview_phase
            else self.fixture_response.get("raise")
        )
        if error:
            mapping = {
                "authentication": ModelAuthenticationError,
                "timeout": ModelTimeoutError,
                "rate_limit": ModelRateLimitError,
                "server": ModelServerError,
                "api": ModelCallError,
            }
            raise mapping.get(str(error), ModelCallError)(f"Fake 模型错误：{error}")
        if overview_phase:
            if "overview_raw" in self.fixture_response:
                return self._response(str(self.fixture_response["overview_raw"]), request)
            payload = self.fixture_response.get("overview")
            if payload is None:
                raise ModelCallError("Fake Fixture 缺少 retrieval_overview 响应")
        elif request.phase == "visual_batch":
            responses = self.fixture_response.get("visual_batches") or []
            if self.batch_index < len(responses):
                payload = responses[self.batch_index]
            else:
                payload = {
                    "batch_summary": "Fake 模型已检查该批页面。",
                    "pages_reviewed": [page["page"] for page in request.pages],
                    "evidence": [],
                    "limitations": [],
                }
            self.batch_index += 1
        elif "raw" in self.fixture_response:
            raw = str(self.fixture_response["raw"])
            return self._response(raw, request)
        else:
            payload = self.fixture_response.get("review")
            if payload is None:
                raise ModelCallError("Fake Fixture 缺少 review 响应")
        return self._response(json.dumps(payload, ensure_ascii=False), request)

    @staticmethod
    def _response(content: str, request: ModelRequest) -> ModelResponse:
        image_bytes = sum(int(page.get("byte_size", 0)) for page in request.pages)
        record = ModelCallRecord(
            phase=request.phase,
            model="fake-kimi",
            attempt_count=1,
            elapsed_ms=0,
            prompt_chars=len(request.prompt),
            image_count=len(request.pages),
            image_bytes=image_bytes,
            finish_reason="stop",
            token_usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )
        return ModelResponse(content=content, record=record)


class KimiMultimodalModel:                        #真实模型，调用Kimi API
    def __init__(self, settings: Settings, *, sleeper: Callable[[float], None] = time.sleep) -> None:
        if settings.kimi_api_key is None:
            raise ModelAuthenticationError("真实模型需要 KIMI_API_KEY 或 MOONSHOT_API_KEY")
        self.settings = settings
        self.sleeper = sleeper
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ModelCallError("缺少 openai 依赖") from exc
        self.client = OpenAI(
            api_key=settings.kimi_api_key.get_secret_value(),
            base_url=settings.kimi_base_url,
            timeout=settings.kimi_timeout_seconds,
            max_retries=0,
        )

    def invoke(self, request: ModelRequest) -> ModelResponse:   #调用Kimi API
        content = self._content_parts(request)
        started = time.perf_counter()
        last_error: BaseException | None = None
        for attempt in range(1, self.settings.kimi_max_attempts + 1):
            try:
                completion = self.client.chat.completions.create(
                    model=request.model or self.settings.kimi_model,
                    reasoning_effort=self.settings.kimi_reasoning_effort,
                    max_completion_tokens=self.settings.kimi_max_completion_tokens,
                    messages=[
                        {"role": "system", "content": request.system_prompt},
                        {"role": "user", "content": content},
                    ],
                    response_format={                #规定模型输出格式
                        "type": "json_schema",
                        "json_schema": {
                            "name": request.schema_name,
                            "strict": True,
                            "schema": strict_json_schema(request.json_schema),
                        },                                                  
                    },
                )
                choice = completion.choices[0]
                text = choice.message.content or ""
                usage_obj = getattr(completion, "usage", None)
                usage = {
                    "prompt_tokens": int(getattr(usage_obj, "prompt_tokens", 0) or 0),
                    "completion_tokens": int(getattr(usage_obj, "completion_tokens", 0) or 0),
                    "total_tokens": int(getattr(usage_obj, "total_tokens", 0) or 0),
                }
                elapsed = int((time.perf_counter() - started) * 1000)
                record = ModelCallRecord(#创建调用记录
                    phase=request.phase,
                    model=request.model or self.settings.kimi_model,
                    attempt_count=attempt,
                    elapsed_ms=elapsed,
                    prompt_chars=len(request.prompt),
                    image_count=len(request.pages),
                    image_bytes=sum(int(page.get("byte_size", 0)) for page in request.pages),
                    finish_reason=str(choice.finish_reason or ""),
                    token_usage=usage,
                )
                return ModelResponse(content=text, finish_reason=str(choice.finish_reason or ""), token_usage=usage, record=record)
            except BaseException as exc:
                converted = self._convert_error(exc)
                last_error = converted
                if not converted.retryable or attempt >= self.settings.kimi_max_attempts:
                    raise converted from exc
                self.sleeper(min(2 ** (attempt - 1), 8))
        raise ModelCallError("Kimi 调用失败") from last_error

#最终组合
    def _content_parts(self, request: ModelRequest) -> list[dict[str, Any]]:#把 Prompt 和 PNG 组成多模态消息
        parts: list[dict[str, Any]] = [{"type": "text", "text": request.prompt}]
        total = len(request.prompt.encode("utf-8"))
        for page in request.pages:
            path = Path(page["path"])
            raw = path.read_bytes()          #png原始字节
            total += len(raw)
            if total > self.settings.max_request_bytes:
                raise ModelCallError("多模态请求体超过安全上限")
            mime = mimetypes.guess_type(path.name)[0] or "image/png"   #判断MIME类型
            encoded = base64.b64encode(raw).decode("ascii")
            parts.append(
                {
                    "type": "text",
                    "text": (
                        f"视觉证据 {page['evidence_id']}，页码 {page['page']}，"
                        f"类型 {page.get('type', 'full_page')}。下一项是对应图片。"
                    ),
                }
            )
            parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
        return parts

    @staticmethod
    def _convert_error(exc: BaseException) -> ModelCallError:
        name = type(exc).__name__
        status = getattr(exc, "status_code", None)
        if name in {"AuthenticationError", "PermissionDeniedError"} or status in {401, 403}:
            return ModelAuthenticationError("Kimi 认证失败")
        if name in {"APITimeoutError", "TimeoutError"}:
            return ModelTimeoutError("Kimi 请求超时")
        if name == "RateLimitError" or status == 429:
            return ModelRateLimitError("Kimi 请求触发限流")
        if name == "APIConnectionError" or (isinstance(status, int) and status >= 500):
            return ModelServerError("Kimi 服务暂时不可用")
        return ModelCallError("Kimi 请求失败")


def request_summary(request: ModelRequest) -> dict[str, Any]:
    return {
        "phase": request.phase,
        "model": request.model or "default",
        "prompt_chars": len(request.prompt),
        "image_count": len(request.pages),
        "image_bytes": sum(int(page.get("byte_size", 0)) for page in request.pages),
        "images": [
            {
                "evidence_id": page["evidence_id"],
                "page": page["page"],
                "width": page["width"],
                "height": page["height"],
                "byte_size": page["byte_size"],
                "sha256": page["sha256"],
                "type": page.get("type", "full_page"),
                "bbox": page.get("bbox"),
            }
            for page in request.pages
        ],
        "schema_name": request.schema_name,
    }


def strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:#规定输出格式
    """Return a copy suitable for Kimi/OpenAI strict structured output."""
    value = json.loads(json.dumps(schema))

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and isinstance(node.get("properties"), dict):
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return value
