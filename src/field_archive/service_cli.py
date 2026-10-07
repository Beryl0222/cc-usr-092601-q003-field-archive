"""治理服务命令行：服务恢复、历史审计与公众脱敏视图。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .audit import (
    build_state_as_of,
    conflicting_viewpoints_at,
    evidence_sources_at,
    place_names_at,
    public_scope_at,
)
from .public_api import public_archive
from .service import GovernanceService, ServiceError, parse_ts
from .store import EventStore


def _print_json(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="field-archive-service", description="青年遗产田野资料治理服务命令行"
    )
    parser.add_argument("store", help="事件日志文件路径(JSONL)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_recover = sub.add_parser("recover", help="服务恢复：继续授权到期与待复核任务")
    p_recover.add_argument("--now", required=True, help="恢复时间(ISO 格式，带时区)")

    for name, helptext in (
        ("audit-place-names", "按历史日期重建地点称谓"),
        ("audit-evidence", "按历史日期重建证据来源"),
        ("audit-conflicts", "按历史日期重建冲突观点"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--place", required=True, help="地点标识(可为已合并的旧地点)")
        p.add_argument("--at", required=True, help="历史日期(ISO 格式，带时区)")

    p_scope = sub.add_parser("audit-public-scope", help="按历史日期重建当时可用的公开范围")
    p_scope.add_argument("--at", required=True, help="历史日期(ISO 格式，带时区)")

    p_public = sub.add_parser("public-archive", help="向公众提供的脱敏档案")
    p_public.add_argument("--at", default=None, help="缺省为当前时间")

    args = parser.parse_args(argv)
    try:
        store = EventStore(args.store)
        if args.command == "recover":
            result = GovernanceService(store).recover(args.now)
        elif args.command == "audit-place-names":
            # 称谓按有效期回答“那一天叫什么”，使用全量沿革知识；
            # timeline 中的 recorded_at 供研究者核对记录时间。
            state = build_state_as_of(store.events(), _latest_occurred_at(store))
            result = place_names_at(state, args.place, args.at)
        else:
            at = args.at or datetime.now(timezone.utc).isoformat()
            state = build_state_as_of(store.events(), at)
            if args.command == "audit-evidence":
                result = evidence_sources_at(state, args.place)
            elif args.command == "audit-conflicts":
                result = conflicting_viewpoints_at(state, args.place)
            elif args.command == "audit-public-scope":
                result = public_scope_at(state, args.at)
            else:
                result = public_archive(state, at)
    except ServiceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _print_json(result)
    return 0


def _latest_occurred_at(store: EventStore) -> str:
    events = store.events()
    if not events:
        return datetime.now(timezone.utc).isoformat()
    return max(events, key=lambda event: parse_ts(event["occurred_at"]))["occurred_at"]


if __name__ == "__main__":
    raise SystemExit(main())
