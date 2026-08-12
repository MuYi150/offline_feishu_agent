from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .errors import OutputConflictError, ReviewHistoryError
from .models import ReviewHistoryRecord


SCHEMA_VERSION = "2.0"


def atomic_write_text(path: Path, text: str, *, allow_identical: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if allow_identical and path.read_text(encoding="utf-8") == text:
            return
        raise OutputConflictError(f"输出文件已存在，拒绝覆盖：{path.name}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, payload: Any, *, allow_identical: bool = False) -> None:
    if isinstance(payload, dict) and "schema_version" not in payload:
        payload = {"schema_version": SCHEMA_VERSION, **payload}
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, text, allow_identical=allow_identical)


def atomic_replace_json(path: Path, payload: Any) -> None:
    if isinstance(payload, dict) and "schema_version" not in payload:
        payload = {"schema_version": SCHEMA_VERSION, **payload}
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}_{os.urandom(4).hex()}"


class AuditStore:
    def save_failure(self, output_dir: Path, failure: dict[str, Any], trace: list[dict[str, Any]]) -> None:
        atomic_replace_json(output_dir / "failure.json", failure)
        atomic_replace_json(output_dir / "run_trace.json", {"events": trace, "failure": failure})


class ReviewHistoryStore:
    def __init__(self, state_root: Path) -> None:
        self.root = state_root

    def load_latest(self, document_id: str) -> ReviewHistoryRecord | None:
        records = self._load_records(document_id)
        if not records:
            return None
        try:
            return ReviewHistoryRecord.model_validate(records[-1])
        except ValidationError as exc:
            raise ReviewHistoryError("最新审稿历史记录不符合 Schema，无法安全判断审稿轮次。") from exc

    def record_count(self, document_id: str) -> int:
        return len(self._load_records(document_id))

    def append(self, document_id: str, record: dict[str, Any] | ReviewHistoryRecord) -> ReviewHistoryRecord:
        try:
            validated = (
                record
                if isinstance(record, ReviewHistoryRecord)
                else ReviewHistoryRecord.model_validate(record)
            )
        except ValidationError as exc:
            raise ReviewHistoryError("待写入的审稿历史记录不符合 Schema。") from exc
        current = self._load_document(document_id)
        serialized = validated.model_dump(mode="json")
        for existing in current["records"]:
            if not isinstance(existing, dict) or existing.get("run_id") != validated.run_id:
                continue
            try:
                existing_validated = ReviewHistoryRecord.model_validate(existing)
            except ValidationError as exc:
                raise ReviewHistoryError("相同 run_id 的既有历史记录不符合 Schema。") from exc
            if existing_validated.model_dump(mode="json") == serialized:
                return existing_validated
            raise ReviewHistoryError("相同 run_id 已存在不同的审稿历史记录，拒绝覆盖。")
        current["records"].append(serialized)
        atomic_replace_json(self._path(document_id), current)
        return validated

    def _load_records(self, document_id: str) -> list[Any]:
        return self._load_document(document_id)["records"]

    def _load_document(self, document_id: str) -> dict[str, Any]:
        path = self._path(document_id)
        if not path.exists():
            return {"schema_version": SCHEMA_VERSION, "document_id": document_id, "records": []}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReviewHistoryError("本地审稿历史文件损坏或无法读取。") from exc
        if not isinstance(loaded, dict) or not isinstance(loaded.get("records"), list):
            raise ReviewHistoryError("本地审稿历史文件结构不符合 Schema。")
        if loaded.get("document_id") != document_id:
            raise ReviewHistoryError("本地审稿历史 document_id 与当前文档不一致。")
        return loaded

    def _path(self, document_id: str) -> Path:
        return self.root / "review_history" / f"{_safe_name(document_id)}.json"


def _safe_name(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value)
    return safe[:120] or "unknown"
