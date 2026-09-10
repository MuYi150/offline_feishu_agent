from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Any

from ..models import SourceDocument

COLUMNS = ["序号", "文档名称", "文档链接", "文档所属知识库", "投稿人", "审稿方式", "审稿人",
           "当前文档状态", "审稿轮次", "文档更新时间", "上次AI审稿时间", "公示时间", "node_token", "obj_token"]
STATUSES = ["已投稿", "待分配人工审稿", "人工审稿中", "AI审稿中", "需修改", "已修改待复审",
            "AI通过待确认", "已公示", "已移出待审核", "已拒稿"]
AI_METHODS = {"AI", "人工+AI"}
AI_STATES = {"AI审稿中", "已修改待复审"}


def text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or value.get("token") or "").strip()
    if isinstance(value, list):
        return "".join(text(item) for item in value)
    return str(value if value is not None else "").strip()


def link(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("link") or "") or next((v for x in value.values() if (v := link(x))), "")
    if isinstance(value, list):
        return next((v for x in value if (v := link(x))), "")
    match = re.search(r'https?://[^\s"\)\']+', str(value or ""))
    return match.group(0).rstrip("/") if match else ""


def token(value: Any) -> str:
    match = re.search(r"/wiki/([A-Za-z0-9_-]+)", link(value) or text(value))
    if match:
        return match.group(1)
    raw = text(value)
    return raw if re.fullmatch(r"[A-Za-z0-9_-]{10,}", raw) else ""


def time_value(value: Any) -> datetime | None:
    raw = text(value)
    if not raw:
        return None
    try:
        number = float(raw)
        if 20000 <= number <= 80000:
            return datetime(1899, 12, 30) + timedelta(days=number)
        if number > 1e9:
            return datetime.fromtimestamp(number / 1000 if number > 1e11 else number)
    except (ValueError, OverflowError, OSError):
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("/", "-").replace("Z", "+00:00"))
        return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return None


def timestamp(value: Any) -> str:
    parsed = time_value(value)
    return parsed.strftime("%Y/%m/%d %H:%M:%S") if parsed else text(value)


def newer(left: Any, right: Any) -> bool:
    a, b = time_value(left), time_value(right)
    return bool(a and b and a > b)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def rows_to_records(rows: list[list]) -> list[dict]:
    if not rows or [text(x) for x in rows[0][:14]] != COLUMNS:
        raise ValueError("文档上传情况表 A:N 表头不符合约定，停止写入")
    records = []
    for row_index, raw in enumerate(rows[1:], 2):
        row = list(raw[:14]) + [""] * max(0, 14 - len(raw))
        if not any(text(x) for x in row[1:]):
            continue
        try:
            number = float(text(row[8]) or "0")
            rounds = int(number)
            if number != rounds:
                raise ValueError("fractional round")
        except ValueError:
            raise ValueError(f"第 {row_index} 行审稿轮次非法")
        if rounds < 0:
            raise ValueError(f"第 {row_index} 行审稿轮次非法")
        method = text(row[5]).replace("＋", "+").replace(" ", "")
        method = {"ai": "AI", "人工+ai": "人工+AI"}.get(method, method)
        status = {"待分配审稿": "待分配人工审稿", "已驳回": "已拒稿"}.get(text(row[7]), text(row[7]))
        records.append(dict(row=row, row_index=row_index, title=text(row[1]), link=link(row[2]),
                            wiki_name=text(row[3]), author=text(row[4]), method=method, reviewer=text(row[6]),
                            status=status, round=rounds, updated=timestamp(row[9]), reviewed=timestamp(row[10]),
                            published=text(row[11]), node=text(row[12]) or token(row[2]), obj=text(row[13])))
    return records


def published(record: dict) -> bool:
    return bool(record["published"] or record["status"] == "已公示")


def eligible(record: dict) -> bool:
    return record["method"] in AI_METHODS and record["status"] in AI_STATES and not published(record) and bool(record["node"] and record["obj"])


def expected_status(record: dict, *, handoff: bool = False, pending: bool = False) -> str:
    if published(record):
        return "已公示"
    if handoff and record["method"] == "AI":
        return "待分配人工审稿"
    if pending or record["status"] == "已移出待审核":
        return record["status"]
    changed = newer(record["updated"], record["reviewed"])
    if record["status"] == "已拒稿":
        return "AI审稿中" if changed and record["method"] in AI_METHODS else "已拒稿"
    if record["status"] == "AI通过待确认":
        return "已修改待复审" if changed and record["method"] == "AI" else record["status"]
    if not record["method"]:
        return "已投稿"
    if record["method"] in {"人工", "人工+AI"}:
        if record["status"] in {"需修改", "已修改待复审"}:
            return record["status"]
        return "人工审稿中" if record["reviewer"] else "待分配人工审稿"
    if record["method"] == "AI":
        if not time_value(record["reviewed"]):
            return "AI审稿中"
        if changed:
            return "已修改待复审" if record["status"] == "需修改" else record["status"]
        return "需修改"
    return record["status"]


def source_document(record: dict, case_id: str, author_id: str = "") -> SourceDocument:
    return SourceDocument(case_id=case_id, document_id=record["obj"], node_token=record["node"],
                          title=record["title"], wiki_name=record["wiki_name"], author_id=author_id,
                          author=record["author"], link=record["link"], review_method=record["method"],
                          status=record["status"], review_round=record["round"], updated_at=record["updated"],
                          last_ai_review_at=record["reviewed"])
