"""追加式事件日志：按事件标识幂等，内容不一致即冲突。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .contracts import load_default_schema, validate_event


class EventIdConflictError(Exception):
    """相同事件标识但内容不一致。"""


class ContractViolationError(Exception):
    """事件不满足交换契约。"""

    def __init__(self, issues):
        self.issues = list(issues)
        message = "；".join(f"{issue.field}: {issue.message}" for issue in self.issues)
        super().__init__(message)


def _canonical(event: Mapping[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False, sort_keys=True)


class EventStore:
    """JSONL 事件日志。

    相同 ``event_id`` 且内容完全一致的重试视为幂等，直接返回已存事件；
    相同 ``event_id`` 但内容不一致说明调用方重用标识，拒绝写入。
    """

    def __init__(self, path, schema: Mapping[str, Any] | None = None):
        self.path = Path(path)
        self.schema = schema if schema is not None else load_default_schema()
        self._events: list[dict[str, Any]] = []
        self._index: dict[str, str] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                self._events.append(event)
                self._index[event["event_id"]] = _canonical(event)

    def events(self) -> list[dict[str, Any]]:
        """按到达顺序返回全部事件。"""
        return list(self._events)

    def append(self, event: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        """校验并追加事件；返回 (事件, 是否新写入)。"""
        issues = validate_event(event, self.schema)
        if issues:
            raise ContractViolationError(issues)
        canonical = _canonical(event)
        existing = self._index.get(event["event_id"])
        if existing is not None:
            if existing == canonical:
                return dict(event), False
            raise EventIdConflictError(f"事件标识 {event['event_id']} 已存在但内容不一致")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        stored = dict(event)
        self._events.append(stored)
        self._index[event["event_id"]] = canonical
        return stored, True
