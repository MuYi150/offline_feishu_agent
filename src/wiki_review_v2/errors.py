from __future__ import annotations


class ReviewError(RuntimeError):
    """Base error with a safe, stable classification."""

    code = "review_error"
    retryable = False

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.safe_message = message
        self.detail = detail


class FixtureError(ReviewError):
    code = "fixture_error"


class PdfError(ReviewError):
    code = "pdf_error"


class InputLimitError(ReviewError):
    code = "input_limit_exceeded"


class ModelCallError(ReviewError):
    code = "model_call_error"


class ModelAuthenticationError(ModelCallError):
    code = "model_authentication_error"


class ModelTimeoutError(ModelCallError):
    code = "model_timeout"
    retryable = True


class ModelRateLimitError(ModelCallError):
    code = "model_rate_limit"
    retryable = True


class ModelServerError(ModelCallError):
    code = "model_server_error"
    retryable = True


class ModelOutputError(ReviewError):
    code = "model_output_error"


class ModelJsonError(ModelOutputError):
    code = "model_non_json"


class ModelSchemaError(ModelOutputError):
    code = "model_schema_error"


class OutputConflictError(ReviewError):
    code = "output_conflict"


def classify_exception(exc: BaseException) -> dict[str, object]:
    if isinstance(exc, ReviewError):
        return {
            "type": type(exc).__name__,
            "code": exc.code,
            "message": exc.safe_message,
            "retryable": exc.retryable,
        }
    return {
        "type": type(exc).__name__,
        "code": "unexpected_error",
        "message": "运行遇到未预期错误，请查看本地调试信息。",
        "retryable": False,
    }

