"""基于 JSONL 的事件存储与可重放投影。

每行一个事件信封：
    {"event_id","event_type","aggregate_type","aggregate_id","occurred_at","version","payload"}

存储只负责条件追加（乐观并发）、事件标识幂等与重放；业务规则在 service 层。
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .contracts import validate_event
from .errors import ContractViolation, VersionConflict


def now_iso() -> str:
    """当前 UTC 时间，显式携带时区。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_ts(value: str) -> datetime:
    """解析携带时区的时间字符串。"""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"时间缺少时区: {value}")
    return parsed


@dataclass(frozen=True)
class Event:
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: str
    version: int
    payload: Mapping[str, Any]
    seq: int = 0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Event":
        return cls(
            event_id=data["event_id"],
            event_type=data["event_type"],
            aggregate_type=data["aggregate_type"],
            aggregate_id=data["aggregate_id"],
            occurred_at=data["occurred_at"],
            version=data["version"],
            payload=dict(data["payload"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "occurred_at": self.occurred_at,
            "version": self.version,
            "payload": dict(self.payload),
        }


class EventStore:
    """线程安全的 JSONL 事件存储。

    - append 使用 expected_version 做条件追加（0 表示新聚合）。
    - 相同 event_id 的重复提交直接返回旧事件（提交幂等）。
    - 服务重启后重新 load_all 即可恢复全部状态与待办。
    """

    def __init__(self, path: str | Path, schema: Mapping[str, Any] | None = None) -> None:
        self.path = Path(path)
        self._schema = schema
        self._lock = threading.RLock()
        self._streams: dict[str, list[Event]] = defaultdict(list)
        self._order: list[Event] = []
        self._by_event_id: dict[str, Event] = {}
        if self.path.exists():
            self._load()

    @property
    def schema(self) -> Mapping[str, Any] | None:
        return self._schema

    def _load(self) -> None:
        for seq, line in enumerate(self.path.read_text(encoding="utf-8").splitlines()):
            line = line.strip()
            if not line:
                continue
            event = Event.from_dict(json.loads(line))
            object.__setattr__(event, "seq", seq)
            self._streams[event.aggregate_id].append(event)
            self._order.append(event)
            self._by_event_id[event.event_id] = event

    def append(
        self,
        event: Event,
        *,
        expected_version: int = 0,
    ) -> Event:
        with self._lock:
            duplicate = self._by_event_id.get(event.event_id)
            if duplicate is not None:
                return duplicate
            if self._schema is not None:
                issues = validate_event(event.to_dict(), self._schema)
                if issues:
                    detail = "; ".join(f"{i.field}:{i.code}" for i in issues)
                    raise ContractViolation(detail)
            current = len(self._streams[event.aggregate_id])
            if event.version != current + 1:
                raise VersionConflict(
                    f"聚合 {event.aggregate_id} 版本冲突: 期望 {current + 1}, 收到 {event.version}"
                )
            if expected_version != current:
                raise VersionConflict(
                    f"聚合 {event.aggregate_id} 已在版本 {current}, 调用方基于 {expected_version}"
                )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
            stored = Event(**event.to_dict(), seq=len(self._order))
            self._streams[event.aggregate_id].append(stored)
            self._order.append(stored)
            self._by_event_id[event.event_id] = stored
            return stored

    def stream(self, aggregate_id: str) -> list[Event]:
        with self._lock:
            return list(self._streams.get(aggregate_id, ()))

    def version(self, aggregate_id: str) -> int:
        with self._lock:
            return len(self._streams.get(aggregate_id, ()))

    def load_all(self, *, until: datetime | None = None) -> list[Event]:
        """按全局追加顺序重放事件（乱序到达的历史补录不改变既有状态）；until 用于历史审计。"""
        with self._lock:
            events = list(self._order)
        if until is not None:
            events = [e for e in events if parse_ts(e.occurred_at) <= until]
        return events
