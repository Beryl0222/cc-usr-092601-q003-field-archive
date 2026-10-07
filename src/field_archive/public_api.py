"""面向公众的脱敏档案视图。

只输出已发布且未冻结的公开快照；姓名、声音、画面按查询时刻的授权
范围脱敏，未成年人材料缺少监护授权或独立复核时一律不公开。
"""

from __future__ import annotations

from typing import Any, Mapping

from .service import MEDIA_SCOPE_REQUIREMENTS, active_place_names

REDACTED_NAME = "匿名受访者"


def redact_asset(state: Mapping[str, Any], asset: Mapping[str, Any]) -> dict[str, Any] | None:
    """按当前授权状态脱敏单条资料；不满足公开条件时返回 None。"""
    consents = state["consents"]
    needed = set(MEDIA_SCOPE_REQUIREMENTS.get(asset["media_type"], ()))
    attribution: list[str] = []
    for subject in asset["subjects"]:
        consent = consents.get(subject)
        if consent is None or "internal_research" not in consent["scope"]:
            return None
        if not needed <= consent["scope"]:
            return None
        if consent.get("is_minor"):
            guardian_ok = bool(consent.get("guardian")) and needed <= set(
                consent.get("guardian_scope", set())
            )
            independent_ok = any(
                review["target_ref"] == asset["business_key"]
                and review["review_kind"] == "independent"
                and review["outcome"] == "approved"
                for review in state["reviews"]
            )
            if not (guardian_ok and independent_ok):
                return None
        attribution.append(subject if "name_public" in consent["scope"] else REDACTED_NAME)
    return {
        "asset": asset["business_key"],
        "content_hash": asset["content_hash"],
        "captured_at": asset["captured_at"],
        "media_type": asset["media_type"],
        "place_ref": asset.get("place_ref"),
        "attribution": attribution,
        "transcriptions": [
            {"transcription_version": t["transcription_version"], "text_hash": t["text_hash"]}
            for t in sorted(
                state["transcriptions"].get(asset["business_key"], []),
                key=lambda t: t["transcription_version"],
            )
        ],
    }


def public_archive(state: Mapping[str, Any], at: str) -> dict[str, Any]:
    """公众脱敏档案：最新一份未冻结公开快照的脱敏内容。"""
    candidates = [
        snapshot
        for snapshot in state["snapshots"].values()
        if snapshot["visibility"] == "public" and not snapshot["frozen"]
    ]
    latest = max(candidates, key=lambda s: (s["occurred_at"], s["snapshot"]), default=None)
    entries: list[dict[str, Any]] = []
    if latest is not None:
        for key in latest["asset_refs"]:
            asset = state["assets"].get(key)
            if asset is None:
                continue
            redacted = redact_asset(state, asset)
            if redacted is None:
                continue
            place_ref = asset.get("place_ref")
            if place_ref:
                redacted["place_names"] = [
                    item["name"] for item in active_place_names(state, place_ref, at)
                ]
            entries.append(redacted)
    return {
        "generated_at": at,
        "snapshot": latest["snapshot"] if latest else None,
        "cutoff_at": latest["cutoff_at"] if latest else None,
        "entries": entries,
    }
