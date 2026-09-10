from __future__ import annotations

import json
import re
from pathlib import Path

from ..models import PreviousIssue, ReviewOutcome
from .records import digest, timestamp
from .snapshots import SnapshotBuilder


def migrate(service, legacy_root: Path, *, write=False):
    """Read V1 JSON only. No imports, configuration reuse or modification of V1."""
    data_dir = legacy_root / "data" if (legacy_root / "data").is_dir() else legacy_root
    filenames = {"history": "ai_review_history.json", "notify": "notify_state.json",
                 "failure": "ai_review_delivery_failures.json"}
    inputs, hashes = {}, {}
    for kind, name in filenames.items():
        path = data_dir / name
        payload = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
        if not isinstance(payload, dict):
            raise ValueError(f"V1 {name} 格式不是对象")
        inputs[kind], hashes[name] = payload, digest(payload)
    report = {"source": str(data_dir.resolve()), "hashes": hashes, "migrated": [], "paused": [], "notifications": []}
    records = service.records()
    known = {r["node"] for r in records}
    for r in records:
        node = r["node"]
        old = inputs["history"].get(node)
        reason, legacy = "", None
        try:
            if inputs["failure"].get(node):
                raise ValueError("V1 存在未确认投递，需人工核对表格和消息")
            if r["round"]:
                if not old:
                    raise ValueError("缺少 V1 真实审稿历史")
                SnapshotBuilder.verify_node(r, service.api.node(node))
                if int(old.get("last_review_round", -1)) != r["round"] or timestamp(old.get("last_review_time")) != r["reviewed"]:
                    raise ValueError("V1 历史轮次或审稿时间与表格不一致")
                result = ReviewOutcome(old["last_result"])
                if not isinstance(old.get("last_issues"), list):
                    raise ValueError("V1 问题列表缺失")
                issues = []
                for i, issue in enumerate(old["last_issues"]):
                    adapted = {k: issue[k] for k in PreviousIssue.model_fields if k in issue and k != "evidence_ids"}
                    adapted["issue_id"] = issue.get("issue_id") or "v1-" + digest([node, r["round"], i, issue])[:20]
                    # V1 evidence references are not V2 visual evidence; retain originals in archive.
                    issues.append(PreviousIssue.model_validate(adapted).model_dump(mode="json"))
                legacy = {"legacy": True, "run_id": "v1-" + digest([node, old])[:24],
                          "review_round": r["round"], "completed_at": r["reviewed"],
                          "result": result.value, "issues": issues}
                report["migrated"].append(node)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            report["paused"].append({"node": node, "reason": reason})
        if write:
            control = service.state.control(node)
            if reason:
                service.state.put("control", node, dict(control, paused=reason))
                service.admin_event(f"migration:{node}:{digest(reason)}", f"V1迁移待处理\n{r['title']}\n{r['link']}\n{reason}")
            elif legacy:
                from .state import OnlineHistoryStore
                OnlineHistoryStore(service.state, node, control["cycle"]).append(r["obj"], legacy)
    for node in set(inputs["history"]) | set(inputs["failure"]):
        if node not in known:
            report["paused"].append({"node": node, "reason": "线上身份无法匹配，仅归档"})
    for key, value in inputs["notify"].items():
        if not isinstance(value, dict) or not value.get("last_notified_at"):
            continue
        rec = next((r for r in records if r["node"] == value.get("node_token")), None)
        if not rec:
            continue
        mapped = None
        key_round = re.search(r":round=(\d+):last_ai=(.*)$", key)
        old_round = value.get("round", int(key_round[1]) if key_round else None)
        old_time = value.get("last_ai_review_at", key_round[2] if key_round else None)
        if value.get("event_type") == "ai_pass_pending" and rec["round"] == old_round and rec["reviewed"] == timestamp(old_time):
            mapped = f"pass:{rec['node']}:{rec['round']}:{rec['reviewed']}"
        elif value.get("event_type") == "missing_review_mode" and rec["status"] == "已投稿" and not rec["method"]:
            mapped = f"assign:{rec['node']}:{rec['updated']}"
        if mapped:
            report["notifications"].append(mapped)
            if write:
                service.state.put("notified", mapped, True)
    if write:
        migration_id = digest(hashes)
        service.state.put("legacy_archive", migration_id, inputs)
        service.state.put("migration_report", migration_id, report)
        service.root.mkdir(parents=True, exist_ok=True)
        (service.root / f"migration-{migration_id[:16]}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    service.event("migration_report", **report)
    return report
