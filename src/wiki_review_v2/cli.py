from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .config import Settings
from .errors import ReviewError, classify_exception
from .runner import ReviewRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="科研团队知识库本地多模态审稿 Agent v2")
    parser.add_argument("--list-cases", action="store_true", help="列出本地 Fixture")
    parser.add_argument("--case", help="运行指定 Fixture case_id")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--fake-model", action="store_true", help="使用离线 Fake 模型（默认）")
    mode.add_argument("--real-model", action="store_true", help="显式调用真实 Kimi 模型")
    parser.add_argument("--output-root", type=Path, help="覆盖默认输出根目录")
    parser.add_argument("--run-id", help="指定新运行 ID；已存在则拒绝覆盖")
    parser.add_argument("--resume", type=Path, help="从已有运行目录的 checkpoint 恢复")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    runner = ReviewRunner(settings)
    if args.list_cases:
        for case_id in runner.list_cases():
            print(case_id)
        return 0
    if args.resume and args.case:
        print(json.dumps({"error": "--resume 与 --case 不能同时使用"}, ensure_ascii=False))
        return 2
    if not args.resume and not args.case:
        print(json.dumps({"error": "请传入 --case、--resume 或 --list-cases"}, ensure_ascii=False))
        return 2
    try:
        if args.resume:
            summary = runner.resume(args.resume, explicit_real_authorization=args.real_model)
        else:
            summary = runner.run_case(  #进行实际工作
                args.case,
                real_model=args.real_model,
                explicit_real_authorization=args.real_model,
                output_root=args.output_root,
                run_id=args.run_id,
            )
    except ReviewError as exc:
        print(json.dumps({"ok": False, "failure": classify_exception(exc)}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "ok": summary.ok,
                "run_id": summary.run_id,
                "result": summary.result,
                "output_dir": str(summary.output_dir),
                "failure": summary.failure,
            },
            ensure_ascii=False,
        )
    )
    return 0 if summary.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

