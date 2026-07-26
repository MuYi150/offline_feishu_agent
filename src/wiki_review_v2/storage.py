from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import OutputConflictError


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

    def append(self, document_id: str, record: dict[str, Any]) -> None:
        path = self.root / "review_history" / f"{_safe_name(document_id)}.json"
        current: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "document_id": document_id, "records": []}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("records"), list):
                    current = loaded
            except (OSError, json.JSONDecodeError):
                corrupt = path.with_suffix(f".corrupt-{utc_run_id()}.json")
                path.replace(corrupt)
        current["records"].append(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(current, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise


def _safe_name(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value)
    return safe[:120] or "unknown"
