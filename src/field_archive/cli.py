"""命令行入口。

用法：
    # 契约校验（保持原有用法）
    python -m field_archive.cli validate <schema.json> <event.json>
    python -m field_archive.cli <schema.json> <event.json>      # 等价于 validate

    # 研究人员审计：按历史日期重建地点称谓、证据来源、冲突观点与当时公开范围
    python -m field_archive.cli audit <事件日志.jsonl> --as-of 2026-05-01T00:00:00+08:00

    # 公众脱敏档案
    python -m field_archive.cli public <事件日志.jsonl> [--as-of ...]

    # 服务恢复后继续推进：授权/资质到期处理与待复核清单
    python -m field_archive.cli run-due <事件日志.jsonl> [--as-of ...]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contracts import validate_event
from .service import FieldArchiveService


def _cmd_validate(args: argparse.Namespace) -> int:
    schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))
    event = json.loads(Path(args.event).read_text(encoding="utf-8"))
    issues = validate_event(event, schema)
    if not issues:
        print("valid")
        return 0
    for issue in issues:
        print(f"{issue.field}	{issue.code}	{issue.message}")
    return 1


def _cmd_audit(args: argparse.Namespace) -> int:
    service = FieldArchiveService(args.store)
    print(json.dumps(service.rebuild_as_of(args.as_of), ensure_ascii=False, indent=2))
    return 0


def _cmd_public(args: argparse.Namespace) -> int:
    service = FieldArchiveService(args.store)
    print(json.dumps(service.public_catalog(args.as_of), ensure_ascii=False, indent=2))
    return 0


def _cmd_run_due(args: argparse.Namespace) -> int:
    service = FieldArchiveService(args.store)
    report = service.run_due(args.as_of)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="field_archive", description="青年遗产田野资料治理命令行")
    sub = parser.add_subparsers(dest="command")

    validate = sub.add_parser("validate", help="校验领域事件信封")
    validate.add_argument("schema")
    validate.add_argument("event")
    validate.set_defaults(func=_cmd_validate)

    audit = sub.add_parser("audit", help="按历史日期重建领域视图（审计命令）")
    audit.add_argument("store", help="JSONL 事件日志路径")
    audit.add_argument("--as-of", required=True, help="历史日期（须带时区）")
    audit.set_defaults(func=_cmd_audit)

    public = sub.add_parser("public", help="输出公众脱敏档案")
    public.add_argument("store", help="JSONL 事件日志路径")
    public.add_argument("--as-of", default=None, help="查询时刻（默认当前，须带时区）")
    public.set_defaults(func=_cmd_public)

    run_due = sub.add_parser("run-due", help="恢复推进：到期处理与待复核任务")
    run_due.add_argument("store", help="JSONL 事件日志路径")
    run_due.add_argument("--as-of", default=None, help="推进时刻（默认当前，须带时区）")
    run_due.set_defaults(func=_cmd_run_due)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # 向后兼容：两个位置参数且无子命令时视为 validate
    if len(argv) == 2 and not argv[0].startswith("-") and argv[0] not in {"validate", "audit", "public", "run-due"}:
        argv = ["validate", *argv]
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help(sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
