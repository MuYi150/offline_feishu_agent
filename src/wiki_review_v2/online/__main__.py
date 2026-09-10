from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
from pathlib import Path

from .config import load_settings
from .feishu import FeishuGateway
from .migration import migrate
from .records import eligible, expected_status
from .service import OnlineService


def parser():
    result = argparse.ArgumentParser(description="V2 飞书线上流程；默认只读，线上修改需 --write，模型需 --real-model")
    result.add_argument("operation", choices=["sync", "review", "run", "retry", "index", "migrate", "diagnose",
        "read-document", "repair-format", "test-message", "deliveries", "resolve-delivery", "release"])
    result.add_argument("--config", type=Path)
    result.add_argument("--write", action="store_true")
    result.add_argument("--real-model", action="store_true")
    result.add_argument("--extract-only", action="store_true")
    result.add_argument("--limit", type=int, default=1)
    result.add_argument("--full", action="store_true", help="index 全量扫描公示文章；仍跳过有效概述")
    result.add_argument("--node-token")
    result.add_argument("--legacy-root", type=Path)
    result.add_argument("--delivery-id")
    result.add_argument("--resolution", choices=["sent", "retry", "cancelled"])
    result.add_argument("--recipient", help="测试消息的明确 open_id")
    result.add_argument("--message", default="V2 飞书机器人消息测试")
    return result


def execute(service, args):
    op = args.operation
    if args.limit < 1:
        raise ValueError("--limit 必须大于 0")
    if args.extract_only and (op not in {"sync", "run"} or args.write or args.real_model):
        raise ValueError("--extract-only 只用于 sync/run，不能与 --write/--real-model 同用")
    if op in {"sync", "run"}:
        service.sync(write=args.write, extract_only=args.extract_only, node=args.node_token)
    if op in {"index", "run"} and not args.extract_only:
        service.index(write=args.write, real_model=args.real_model, limit=args.limit if op == "index" and not args.full else None)
    if op in {"review", "run"} and not args.extract_only:
        if op == "run" and args.write and not args.real_model:
            service.event("review_skipped", reason="需要 --real-model")
        else:
            service.review(write=args.write, real_model=args.real_model, limit=args.limit, node=args.node_token)
    if op in {"sync", "review", "run", "retry"} and not args.extract_only:
        service.retry(write=args.write, node=args.node_token)
    if op == "migrate":
        if not args.legacy_root:
            raise ValueError("migrate 需要 --legacy-root")
        migrate(service, args.legacy_root.resolve(), write=args.write)
    if op == "diagnose":
        for record in service.records():
            if args.node_token and record["node"] != args.node_token:
                continue
            control = service.state.control(record["node"])
            service.event("diagnosis", node=record["node"], row=record["row_index"], status=record["status"],
                          expected=expected_status(record, handoff=control["handoff"], pending=service.state.pending(record["node"])),
                          eligible=eligible(record), control=control, round=record["round"])
    if op in {"read-document", "repair-format", "release"}:
        if not args.node_token:
            raise ValueError(f"{op} 需要 --node-token")
        matches = [r for r in service.records() if r["node"] == args.node_token]
        if len(matches) != 1:
            raise ValueError("文档未唯一匹配表格记录")
        record = matches[0]
        if op == "read-document":
            from .snapshots import BlocksAdapter
            node = service.api.node(record["node"])
            raw = service.api.blocks(node["obj_token"])
            blocks, attachments = BlocksAdapter(service.api).adapt(raw, node["obj_token"])
            service.event("document_diagnosis", node=record["node"], blocks=len(blocks), attachments=attachments,
                          content=blocks, updated_at=node.get("edit_time"))
        elif op == "repair-format":
            service.event("repair_format", row=record["row_index"], node=record["node"])
            if args.write:
                service.api.repair_format(record["row_index"], record["row_index"] - 1 if record["row_index"] > 2 else None)
                service.refresh(record, service.api.node(record["node"]), write=True)
        else:
            service.event("release_migration_pause", node=record["node"])
            if args.write:
                control = service.state.control(record["node"])
                control.pop("paused", None)
                service.state.put("control", record["node"], control)
    if op == "deliveries":
        for key, value in service.state.items("delivery"):
            service.event("delivery", **value)
        for key, value in service.state.items("job"):
            service.event("job", id=key, node=value["node"], status=value["status"])
    if op == "resolve-delivery":
        item = service.state.get("delivery", args.delivery_id)
        if not item or not args.resolution:
            raise ValueError("需要有效 --delivery-id 和 --resolution；uncertain 必须先在线核实发送结果")
        service.event("resolve_delivery", id=args.delivery_id, resolution=args.resolution)
        if args.write:
            item.update(status="pending" if args.resolution == "retry" else args.resolution,
                        manual_resolution=args.resolution)
            service.state.put("delivery", args.delivery_id, item)
    if op == "test-message":
        if not args.recipient or not args.recipient.startswith("ou_"):
            raise ValueError("test-message 需要明确 --recipient ou_...")
        service.event("test_message", recipient=args.recipient, content=args.message)
        if args.write:
            from ..storage import utc_run_id
            event = "test:" + utc_run_id()
            service.enqueue(event, "admin", args.recipient, args.message)
            service.retry(write=True, event=event)


def main(argv=None):
    args = parser().parse_args(argv)
    gateway = None
    try:
        feishu, settings, root = load_settings(args.config)
        if args.operation not in {"deliveries", "resolve-delivery"}:
            feishu.validate()
        gateway = FeishuGateway(feishu)
        service = OnlineService(gateway, feishu, settings, root)
        with service.state.lock() if args.write or args.extract_only or args.real_model else nullcontext():
            execute(service, args)
        print(json.dumps({"write": args.write, "events": service.events}, ensure_ascii=False, indent=2))
        return 1 if any("error" in event for event in service.events) else 0
    except Exception as exc:
        print(json.dumps({"error": getattr(exc, "code", str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__)}, ensure_ascii=False))
        return 1
    finally:
        if gateway:
            gateway.close()


if __name__ == "__main__":
    raise SystemExit(main())
