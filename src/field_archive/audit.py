"""按历史日期重建地点称谓、证据来源、冲突观点与当时可用的公开范围。

审计查询只读取截至指定日期已发生的事件，乱序到达的事件按发生时间
归位；不同代际的叙述与不同边界版本的空间判断各自保留，绝不合并成
一个答案。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .public_api import redact_asset
from .service import (
    active_place_names,
    apply_event,
    new_state,
    parse_ts,
    resolve_place_members,
    surviving_place,
)


def build_state_as_of(events: Iterable[Mapping[str, Any]], until: str) -> dict[str, Any]:
    """用截至 ``until`` 的事件重建状态；按发生时间排序，到达顺序仅作次序兜底。"""
    until_dt = parse_ts(until, "until")
    selected = [
        (index, event)
        for index, event in enumerate(events)
        if parse_ts(event["occurred_at"]) <= until_dt
    ]
    selected.sort(key=lambda pair: (parse_ts(pair[1]["occurred_at"]), pair[0]))
    state = new_state()
    for _, event in selected:
        apply_event(state, event)
    return state


def place_names_at(state: Mapping[str, Any], place_id: str, at: str) -> dict[str, Any]:
    """重建地点称谓：当时有效的名称与完整沿革（含旧称有效期）。"""
    timeline: list[dict[str, Any]] = []
    for pid in resolve_place_members(state, place_id):
        for entry in state["places"].get(pid, {}).get("names", []):
            timeline.append(
                {
                    "name": entry["name"],
                    "valid_from": entry["valid_from"],
                    "valid_to": entry.get("valid_to"),
                    "source_place": pid,
                    "recorded_at": entry.get("recorded_at"),
                }
            )
    timeline.sort(key=lambda item: (item["valid_from"], item["name"]))
    return {
        "place": place_id,
        "surviving_place": surviving_place(state, place_id),
        "at": at,
        "names_active_at": active_place_names(state, place_id, at),
        "timeline": timeline,
    }


def evidence_sources_at(state: Mapping[str, Any], place_id: str) -> dict[str, Any]:
    """重建证据来源：资料摘要、转写版本、空间判断、复核与上传冲突。"""
    members = set(resolve_place_members(state, place_id))
    assets = [asset for asset in state["assets"].values() if asset.get("place_ref") in members]
    assets.sort(key=lambda a: (a["captured_at"], a["business_key"]))
    asset_keys = {asset["business_key"] for asset in assets}
    transcriptions = {
        key: sorted(
            state["transcriptions"][key], key=lambda t: t["transcription_version"]
        )
        for key in sorted(asset_keys)
        if state["transcriptions"].get(key)
    }
    judgments = [
        {**judgment, "place_ref": pid}
        for pid in sorted(members)
        for judgment in state["places"].get(pid, {}).get("judgments", [])
    ]
    judgments.sort(key=lambda j: (j["occurred_at"], j["judgment_id"]))
    return {
        "place": place_id,
        "place_members": sorted(members),
        "assets": [dict(asset) for asset in assets],
        "transcriptions": transcriptions,
        "spatial_judgments": judgments,
        "reviews": [r for r in state["reviews"] if r["target_ref"] in asset_keys],
        "upload_conflicts": [c for c in state["conflicts"] if c.get("business_key") in asset_keys],
    }


def conflicting_viewpoints_at(state: Mapping[str, Any], place_id: str) -> dict[str, Any]:
    """重建冲突观点：按代际与边界版本分组保留，不合并成一个答案。"""
    members = resolve_place_members(state, place_id)
    narratives_by_cohort: dict[str, list[dict[str, Any]]] = {}
    judgments_by_boundary: dict[str, list[dict[str, Any]]] = {}
    summary_hashes: set[str] = set()
    judgments: set[str] = set()
    for pid in members:
        place = state["places"].get(pid, {})
        for narrative in place.get("narratives", []):
            narratives_by_cohort.setdefault(narrative["cohort"], []).append(
                {**narrative, "source_place": pid}
            )
            summary_hashes.add(narrative["summary_hash"])
        for judgment in place.get("judgments", []):
            judgments_by_boundary.setdefault(judgment["boundary_version"], []).append(
                {**judgment, "source_place": pid}
            )
            judgments.add(judgment["judgment"])
    for entries in narratives_by_cohort.values():
        entries.sort(key=lambda n: (n["occurred_at"], n["narrator_ref"]))
    for entries in judgments_by_boundary.values():
        entries.sort(key=lambda j: (j["occurred_at"], j["judgment_id"]))
    return {
        "place": place_id,
        "place_members": sorted(members),
        "narratives_by_cohort": dict(sorted(narratives_by_cohort.items())),
        "judgments_by_boundary_version": dict(sorted(judgments_by_boundary.items())),
        "distinct_narrative_views": len(summary_hashes),
        "distinct_spatial_views": len(judgments),
        "has_conflict": len(summary_hashes) > 1 or len(judgments) > 1,
    }


def public_scope_at(state: Mapping[str, Any], at: str) -> dict[str, Any]:
    """重建当时可用的公开范围：未冻结公开快照的脱敏内容与已冻结清单。"""
    available: list[dict[str, Any]] = []
    frozen: list[dict[str, Any]] = []
    snapshots = sorted(
        state["snapshots"].values(), key=lambda s: (s["occurred_at"], s["snapshot"])
    )
    for snapshot in snapshots:
        if snapshot["visibility"] != "public":
            continue
        base = {
            "snapshot": snapshot["snapshot"],
            "published_at": snapshot["occurred_at"],
            "cutoff_at": snapshot["cutoff_at"],
            "purpose": snapshot.get("purpose"),
        }
        if snapshot["frozen"]:
            frozen.append({**base, "reason": snapshot.get("freeze_reason")})
            continue
        entries = []
        for key in snapshot["asset_refs"]:
            asset = state["assets"].get(key)
            if asset is None:
                continue
            redacted = redact_asset(state, asset)
            if redacted is not None:
                entries.append(redacted)
        available.append({**base, "entries": entries})
    return {"at": at, "available": available, "frozen": frozen}
