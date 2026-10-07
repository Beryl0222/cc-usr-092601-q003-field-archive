"""青年遗产田野资料治理服务。

在事件存储之上提供：
- 培训资质与采集任务（并发认领，唯一提交责任）
- 幂等上传（业务键 + 文件指纹 + 授权范围）
- 地点称谓沿革、地点合并（旧称与有效期保留）、边界修订、代际叙述并列
- 受访者同意授予/更新/收窄/到期；未成年人监护授权与独立复核
- 转写版本、专家复核（退回不改原始资料）
- 档案快照发布与按授权收窄的定向冻结
- 重启后继续处理授权到期与待复核任务
- 公众脱敏档案与研究人员按历史日期的审计重建
"""

from __future__ import annotations

import hashlib
import json
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import (
    CertificationError,
    ConflictError,
    ConsentError,
    ReviewError,
)
from .store import Event, EventStore, now_iso, parse_ts

PUBLIC_SCOPES = ("voice_public", "identity_public", "transcript_public", "image_public")
INTERNAL_SCOPE = "internal_research"

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"


def load_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _new_state() -> dict[str, Any]:
    return {
        "certs": {},
        "assignments": {},
        "places": {},
        "boundaries": {},
        "assets": {},
        "assets_by_key": {},
        "consents": {},
        "transcripts": {},
        "reviews": {},
        "snapshots": {},
    }


def apply(state: dict[str, Any], event: Event) -> None:
    """把单个事件折叠进状态（纯增量，不允许修改既有事实）。"""
    p = event.payload
    t = event.event_type
    if t == "CERTIFICATION_GRANTED":
        state["certs"][event.aggregate_id] = {
            "certification_id": p["certification_id"],
            "collector_ref": p["collector_ref"],
            "training_code": p["training_code"],
            "valid_from": p["valid_from"],
            "valid_until": p.get("valid_until"),
            "status": "valid",
            "expired_at": None,
        }
    elif t == "CERTIFICATION_EXPIRED":
        cert = state["certs"].get(event.aggregate_id)
        if cert is not None:
            cert["status"] = "expired"
            cert["expired_at"] = p["expired_at"]
    elif t == "ASSIGNMENT_REGISTERED":
        state["assignments"][event.aggregate_id] = {
            "assignment_id": event.aggregate_id,
            "assignment_key": p["assignment_key"],
            "required_certification": p["required_certification"],
            "claim": None,
        }
    elif t == "ASSIGNMENT_CLAIMED":
        assignment = state["assignments"].get(event.aggregate_id)
        if assignment is not None and assignment["claim"] is None:
            assignment["claim"] = {
                "collector_ref": p["collector_ref"],
                "certification_id": p["certification_id"],
                "claimed_at": event.occurred_at,
                "event_id": event.event_id,
            }
    elif t == "PLACE_NAMED":
        place = state["places"].setdefault(
            event.aggregate_id, {"place_id": p["place_id"], "names": [], "merges": [], "merged_into": None, "narratives": []}
        )
        place["names"].append(
            {
                "name": p["name"],
                "valid_from": p["valid_from"],
                "valid_until": p.get("valid_until"),
                "event_id": event.event_id,
            }
        )
    elif t == "PLACE_MERGED":
        survivor = state["places"].setdefault(
            event.aggregate_id,
            {"place_id": p["surviving_place_id"], "names": [], "merges": [], "merged_into": None, "narratives": []},
        )
        survivor["merges"].append(
            {"merged_place_ids": list(p["merged_place_ids"]), "effective_at": p["effective_at"], "event_id": event.event_id}
        )
        for merged_id in p["merged_place_ids"]:
            merged = state["places"].setdefault(
                f"place:{merged_id}",
                {"place_id": merged_id, "names": [], "merges": [], "merged_into": None, "narratives": []},
            )
            merged["merged_into"] = {
                "surviving_place_id": p["surviving_place_id"],
                "effective_at": p["effective_at"],
            }
    elif t == "BOUNDARY_REVISED":
        state["boundaries"].setdefault(event.aggregate_id, []).append(
            {"revision_no": p["revision_no"], "effective_at": p["effective_at"], "note": p.get("note"), "event_id": event.event_id}
        )
    elif t == "NARRATIVE_RECORDED":
        place = state["places"].setdefault(
            event.aggregate_id, {"place_id": p["place_ref"], "names": [], "merges": [], "merged_into": None, "narratives": []}
        )
        place["narratives"].append(
            {
                "narrative_id": p["narrative_id"],
                "generation": p["generation"],
                "captured_at": p["captured_at"],
                "asset_ref": p.get("asset_ref"),
                "summary": p.get("summary"),
                "event_id": event.event_id,
            }
        )
    elif t == "ASSET_UPLOADED":
        asset = {
            "asset_id": event.aggregate_id,
            "business_key": p["business_key"],
            "content_hash": p["content_hash"],
            "captured_at": p["captured_at"],
            "consent_scope": p["consent_scope"],
            "kind": p.get("kind", "file"),
            "place_ref": p.get("place_ref"),
            "boundary_ref": p.get("boundary_ref"),
            "boundary_revision": p.get("boundary_revision"),
            "subject_ref": p.get("subject_ref"),
            "summary": p.get("summary"),
            "uploader_ref": p.get("uploader_ref"),
            "uploaded_at": event.occurred_at,
            "event_id": event.event_id,
        }
        state["assets"][event.aggregate_id] = asset
        state["assets_by_key"].setdefault(p["business_key"], event.aggregate_id)
    elif t in ("CONSENT_GRANTED", "CONSENT_UPDATED"):
        record = state["consents"].setdefault(
            event.aggregate_id, {"grants": [], "narrowed": [], "expired_at": None, "is_minor": False, "guardian_subject_ref": None}
        )
        record["grants"].append(
            {
                "scope": p["scope"],
                "at": p.get("granted_at", event.occurred_at),
                "valid_until": p.get("valid_until"),
                "via": t,
                "event_id": event.event_id,
            }
        )
        if p.get("is_minor"):
            record["is_minor"] = True
        if p.get("guardian_subject_ref"):
            record["guardian_subject_ref"] = p["guardian_subject_ref"]
    elif t == "CONSENT_NARROWED":
        record = state["consents"].setdefault(
            event.aggregate_id, {"grants": [], "narrowed": [], "expired_at": None, "is_minor": False, "guardian_subject_ref": None}
        )
        record["narrowed"].append({"scope": p["scope"], "at": p["effective_at"], "event_id": event.event_id})
    elif t == "CONSENT_EXPIRED":
        record = state["consents"].setdefault(
            event.aggregate_id, {"grants": [], "narrowed": [], "expired_at": None, "is_minor": False, "guardian_subject_ref": None}
        )
        record["expired_at"] = p["expired_at"]
    elif t == "TRANSCRIPT_VERSIONED":
        transcript = state["transcripts"].setdefault(event.aggregate_id, {"versions": {}})
        transcript["versions"][p["version_no"]] = {
            "source_asset_id": p["source_asset_id"],
            "text": p.get("text", ""),
            "editor_ref": p.get("editor_ref"),
            "narrative_id": p.get("narrative_id"),
            "at": event.occurred_at,
            "event_id": event.event_id,
        }
    elif t == "REVIEW_OPENED":
        state["reviews"][event.aggregate_id] = {
            "review_case_id": p["review_case_id"],
            "target_ref": p["target_ref"],
            "kind": p.get("kind", "expert"),
            "status": "open",
            "decision": None,
            "rounds": 0,
            "notes": [
                {
                    "cited_boundary_ref": p.get("cited_boundary_ref"),
                    "cited_revision_no": p.get("cited_revision_no"),
                    "current_revision_no": p.get("current_revision_no"),
                    "note": p.get("note"),
                }
            ],
        }
    elif t == "REVIEW_RETURNED":
        review = state["reviews"].get(event.aggregate_id)
        if review is not None:
            review["status"] = "returned"
            review["rounds"] += 1
            review.setdefault("returns", []).append({"reason": p["reason"], "at": event.occurred_at})
    elif t == "REVIEW_COMPLETED":
        review = state["reviews"].get(event.aggregate_id)
        if review is not None:
            review["status"] = "completed"
            review["decision"] = p["decision"]
            review["completed_at"] = event.occurred_at
    elif t == "SNAPSHOT_PUBLISHED":
        state["snapshots"][event.aggregate_id] = {
            "snapshot_id": p.get("snapshot_id", event.aggregate_id),
            "cutoff_at": p["cutoff_at"],
            "visibility": p["visibility"],
            "entries": deepcopy(p.get("entries", [])),
            "published_at": event.occurred_at,
            "freezes": [],
        }
    elif t == "SNAPSHOT_FROZEN":
        snapshot = state["snapshots"].get(event.aggregate_id)
        if snapshot is not None:
            snapshot["freezes"].append(
                {"reason": p["reason"], "frozen_at": p["frozen_at"], "entries": list(p.get("entries", []))}
            )


class FieldArchiveService:
    """事件溯源的治理服务。构造即恢复：传入同一日志路径可重放全部状态。"""

    def __init__(self, store: EventStore | str | Path) -> None:
        if isinstance(store, EventStore):
            self.store = store
        else:
            self.store = EventStore(store, schema=load_schema())
        self._lock = threading.RLock()
        self.state = _new_state()
        for event in self.store.load_all():
            apply(self.state, event)

    # ------------------------------------------------------------------ 内部

    def _append(self, event_type: str, aggregate_type: str, aggregate_id: str, payload: dict[str, Any], at: str) -> Event:
        version = self.store.version(aggregate_id) + 1
        event = Event(
            event_id=f"{event_type}:{aggregate_id}:{version}",
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            occurred_at=at,
            version=version,
            payload=payload,
        )
        stored = self.store.append(event, expected_version=version - 1)
        apply(self.state, stored)
        return stored

    @staticmethod
    def _scopes_at(record: dict[str, Any] | None, moment: datetime, *, include_expiring: bool = False) -> set[str]:
        """按事件历史计算某时刻实际有效的授权范围（以各次授予自身的有效期为准）。

        include_expiring=True 时，恰在 moment 到期的授予仍计入（用于到期瞬间
        判断哪些公开版本需要冻结）。
        """
        if record is None:
            return set()
        held: set[str] = set()
        for grant in record["grants"]:
            if parse_ts(grant["at"]) > moment:
                continue
            expires = parse_ts(grant["valid_until"]) if grant.get("valid_until") else None
            if expires is not None:
                if include_expiring:
                    if moment > expires:
                        continue
                elif moment >= expires:
                    continue
            narrowed = any(
                n["scope"] == grant["scope"]
                and parse_ts(grant["at"]) <= parse_ts(n["at"]) <= moment
                for n in record["narrowed"]
            )
            if not narrowed:
                held.add(grant["scope"])
        # 内部研究的最小事实在到期/收窄后按原依据保留
        if record["grants"]:
            held.add(INTERNAL_SCOPE)
        return held

    def _frozen_entry_refs(self, snapshot: dict[str, Any], moment: datetime | None = None) -> set[str]:
        refs: set[str] = set()
        for freeze in snapshot["freezes"]:
            if moment is not None and parse_ts(freeze["frozen_at"]) > moment:
                continue
            refs.update(freeze["entries"])
        return refs

    def _freeze_for_subject(self, subject_ref: str, lost_scopes: set[str], at: str, reason: str) -> list[Event]:
        """只冻结公开快照中依赖被收窄范围的条目；内部研究事实保留。"""
        events: list[Event] = []
        if not lost_scopes:
            return events
        for snapshot_id, snapshot in self.state["snapshots"].items():
            already = self._frozen_entry_refs(snapshot)
            affected = [
                entry["ref"]
                for entry in snapshot["entries"]
                if entry.get("subject_ref") == subject_ref
                and entry.get("scope") in lost_scopes
                and entry["ref"] not in already
            ]
            if affected:
                events.append(
                    self._append(
                        "SNAPSHOT_FROZEN",
                        "archive_snapshot",
                        snapshot_id,
                        {
                            "snapshot_id": snapshot["snapshot_id"],
                            "reason": reason,
                            "frozen_at": at,
                            "entries": affected,
                        },
                        at,
                    )
                )
        return events

    # ------------------------------------------------------------------ 资质

    def grant_certification(
        self,
        certification_id: str,
        collector_ref: str,
        training_code: str,
        valid_from: str,
        valid_until: str | None = None,
        at: str | None = None,
    ) -> Event:
        at = at or now_iso()
        with self._lock:
            return self._append(
                "CERTIFICATION_GRANTED",
                "collector_profile",
                f"cert:{certification_id}",
                {
                    "certification_id": certification_id,
                    "collector_ref": collector_ref,
                    "training_code": training_code,
                    "valid_from": valid_from,
                    **({"valid_until": valid_until} if valid_until else {}),
                },
                at,
            )

    def _cert_valid(self, certification_id: str, required_training: str, moment: datetime) -> None:
        cert = self.state["certs"].get(f"cert:{certification_id}")
        if cert is None:
            raise CertificationError(f"培训资质 {certification_id} 不存在")
        if cert["training_code"] != required_training:
            raise CertificationError(
                f"资质 {certification_id} 的培训方向 {cert['training_code']} 不满足任务要求 {required_training}"
            )
        if parse_ts(cert["valid_from"]) > moment:
            raise CertificationError(f"资质 {certification_id} 尚未生效")
        if cert["status"] == "expired" or (cert.get("valid_until") and moment >= parse_ts(cert["valid_until"])):
            raise CertificationError(f"资质 {certification_id} 已过期")

    # ------------------------------------------------------------------ 任务

    def register_assignment(self, assignment_key: str, required_certification: str, at: str | None = None) -> Event:
        at = at or now_iso()
        with self._lock:
            aggregate_id = f"assign:{assignment_key}"
            if aggregate_id in self.state["assignments"]:
                return self.store.stream(aggregate_id)[0]
            return self._append(
                "ASSIGNMENT_REGISTERED",
                "field_assignment",
                aggregate_id,
                {"assignment_key": assignment_key, "required_certification": required_certification},
                at,
            )

    def claim_assignment(
        self, assignment_key: str, collector_ref: str, certification_id: str, at: str | None = None
    ) -> Event:
        """并发认领：只有第一个符合资质的学生获得提交责任。

        同一学生重复认领视为重试，幂等返回既有认领事件。
        """
        at = at or now_iso()
        moment = parse_ts(at)
        with self._lock:
            aggregate_id = f"assign:{assignment_key}"
            assignment = self.state["assignments"].get(aggregate_id)
            if assignment is None:
                raise ConflictError(f"采集任务 {assignment_key} 尚未注册")
            claim = assignment["claim"]
            if claim is not None:
                if claim["collector_ref"] == collector_ref:
                    return next(
                        e for e in self.store.stream(aggregate_id) if e.event_type == "ASSIGNMENT_CLAIMED"
                    )
                raise ConflictError(
                    f"任务 {assignment_key} 已由 {claim['collector_ref']} 认领，提交责任不可重复授予"
                )
            self._cert_valid(certification_id, assignment["required_certification"], moment)
            return self._append(
                "ASSIGNMENT_CLAIMED",
                "field_assignment",
                aggregate_id,
                {
                    "assignment_key": assignment_key,
                    "collector_ref": collector_ref,
                    "certification_id": certification_id,
                },
                at,
            )

    # ------------------------------------------------------------------ 地点

    def name_place(
        self,
        place_id: str,
        name: str,
        valid_from: str,
        valid_until: str | None = None,
        at: str | None = None,
    ) -> Event:
        at = at or now_iso()
        with self._lock:
            payload: dict[str, Any] = {"place_id": place_id, "name": name, "valid_from": valid_from}
            if valid_until:
                payload["valid_until"] = valid_until
            return self._append("PLACE_NAMED", "place_record", f"place:{place_id}", payload, at)

    def merge_places(
        self, surviving_place_id: str, merged_place_ids: list[str], effective_at: str, at: str | None = None
    ) -> Event:
        """地点合并不删除旧地点：旧称、有效期与叙述全部保留，仅建立指向关系。"""
        at = at or effective_at
        with self._lock:
            if surviving_place_id in merged_place_ids:
                raise ConflictError("合并后的保留地点不能同时出现在被合并列表中")
            for merged_id in merged_place_ids:
                place = self.state["places"].get(f"place:{merged_id}")
                if place and place["merged_into"]:
                    raise ConflictError(f"地点 {merged_id} 已被合并，不能重复合并")
            return self._append(
                "PLACE_MERGED",
                "place_record",
                f"place:{surviving_place_id}",
                {
                    "surviving_place_id": surviving_place_id,
                    "merged_place_ids": list(merged_place_ids),
                    "effective_at": effective_at,
                },
                at,
            )

    def revise_boundary(
        self, boundary_ref: str, revision_no: int, effective_at: str, note: str | None = None, at: str | None = None
    ) -> Event:
        at = at or effective_at
        with self._lock:
            prior = [b["revision_no"] for b in self.state["boundaries"].get(f"boundary:{boundary_ref}", [])]
            if prior and revision_no <= max(prior):
                raise ConflictError(f"测绘边界 {boundary_ref} 的修订号必须递增")
            payload = {"boundary_ref": boundary_ref, "revision_no": revision_no, "effective_at": effective_at}
            if note:
                payload["note"] = note
            return self._append("BOUNDARY_REVISED", "place_record", f"boundary:{boundary_ref}", payload, at)

    def record_narrative(
        self,
        narrative_id: str,
        place_ref: str,
        generation: str,
        captured_at: str,
        asset_ref: str | None = None,
        summary: str | None = None,
        at: str | None = None,
    ) -> Event:
        """记录某一代人的叙述。不同代际的叙述并列保存，不互相覆盖。"""
        at = at or now_iso()
        with self._lock:
            payload: dict[str, Any] = {
                "narrative_id": narrative_id,
                "place_ref": place_ref,
                "generation": generation,
                "captured_at": captured_at,
            }
            if asset_ref:
                payload["asset_ref"] = asset_ref
            if summary:
                payload["summary"] = summary
            return self._append("NARRATIVE_RECORDED", "place_record", f"place:{place_ref}", payload, at)

    # ------------------------------------------------------------------ 素材

    def upload_asset(
        self,
        asset_id: str,
        business_key: str,
        content_hash: str,
        captured_at: str,
        consent_scope: str,
        *,
        kind: str = "file",
        place_ref: str | None = None,
        boundary_ref: str | None = None,
        boundary_revision: int | None = None,
        subject_ref: str | None = None,
        summary: str | None = None,
        uploader_ref: str | None = None,
        at: str | None = None,
    ) -> Event:
        """乱序重试安全的上传。

        相同业务键只有 content_hash 与 consent_scope 都一致才视为幂等；
        指纹或授权范围不一致直接拒绝，绝不覆盖。
        """
        at = at or now_iso()
        with self._lock:
            existing_id = self.state["assets_by_key"].get(business_key)
            if existing_id is not None:
                existing = self.state["assets"][existing_id]
                if existing["content_hash"] == content_hash and existing["consent_scope"] == consent_scope:
                    return self.store.stream(existing_id)[0]
                raise ConflictError(
                    f"业务键 {business_key} 已存在但文件指纹或授权范围不一致；幂等要求两者完全相同"
                )
            payload: dict[str, Any] = {
                "business_key": business_key,
                "content_hash": content_hash,
                "captured_at": captured_at,
                "consent_scope": consent_scope,
                "kind": kind,
            }
            for key, value in (
                ("place_ref", place_ref),
                ("boundary_ref", boundary_ref),
                ("boundary_revision", boundary_revision),
                ("subject_ref", subject_ref),
                ("summary", summary),
                ("uploader_ref", uploader_ref),
            ):
                if value is not None:
                    payload[key] = value
            return self._append("ASSET_UPLOADED", "source_asset", f"asset:{asset_id}", payload, at)

    # ------------------------------------------------------------------ 授权

    def grant_consent(
        self,
        subject_ref: str,
        scopes: list[str],
        granted_at: str | None = None,
        valid_until: str | None = None,
        *,
        is_minor: bool = False,
        guardian_subject_ref: str | None = None,
        at: str | None = None,
    ) -> list[Event]:
        """授予授权。未成年人必须提供监护人授权引用，且监护人授权须先存在。"""
        at = at or now_iso()
        granted_at = granted_at or at
        with self._lock:
            if is_minor and not guardian_subject_ref:
                raise ConsentError("未成年人材料必须提供监护授权（guardian_subject_ref）")
            if is_minor and guardian_subject_ref:
                guardian = self.state["consents"].get(f"consent:{guardian_subject_ref}")
                if not guardian or not guardian["grants"]:
                    raise ConsentError(f"监护人 {guardian_subject_ref} 的授权记录不存在，监护依据不足")
            events = []
            for scope in scopes:
                payload: dict[str, Any] = {
                    "subject_ref": subject_ref,
                    "scope": scope,
                    "granted_at": granted_at,
                }
                if valid_until:
                    payload["valid_until"] = valid_until
                if is_minor:
                    payload["is_minor"] = True
                    payload["guardian_subject_ref"] = guardian_subject_ref
                events.append(
                    self._append("CONSENT_GRANTED", "consent_record", f"consent:{subject_ref}", payload, at)
                )
            return events

    def narrow_consent(
        self, subject_ref: str, removed_scopes: list[str], effective_at: str | None = None, *, reason: str = ""
    ) -> list[Event]:
        """收窄许可：仅冻结依赖被收窄范围的公开版本，内部研究最小事实保留。"""
        effective_at = effective_at or now_iso()
        with self._lock:
            record = self.state["consents"].get(f"consent:{subject_ref}")
            if record is None:
                raise ConsentError(f"受访者 {subject_ref} 没有授权记录，无法收窄")
            current = self._scopes_at(record, parse_ts(effective_at))
            lost = {s for s in removed_scopes if s in current}
            events = []
            for scope in sorted(lost):
                events.append(
                    self._append(
                        "CONSENT_NARROWED",
                        "consent_record",
                        f"consent:{subject_ref}",
                        {"subject_ref": subject_ref, "scope": scope, "effective_at": effective_at},
                        effective_at,
                    )
                )
            if lost:
                events.extend(
                    self._freeze_for_subject(
                        subject_ref,
                        lost & set(PUBLIC_SCOPES),
                        effective_at,
                        reason or f"受访者 {subject_ref} 收窄授权: {sorted(lost)}",
                    )
                )
            return events

    def _expire_consent(self, subject_ref: str, expired_at: str) -> list[Event]:
        record = self.state["consents"][f"consent:{subject_ref}"]
        moment = parse_ts(expired_at)
        # 到期瞬间：把因到期而失去的公开范围冻结（其他仍在有效期内的授予不受影响）
        lost_public = (
            self._scopes_at(record, moment, include_expiring=True) - self._scopes_at(record, moment)
        ) & set(PUBLIC_SCOPES)
        events = [
            self._append(
                "CONSENT_EXPIRED",
                "consent_record",
                f"consent:{subject_ref}",
                {"subject_ref": subject_ref, "expired_at": expired_at},
                expired_at,
            )
        ]
        events.extend(
            self._freeze_for_subject(subject_ref, lost_public, expired_at, f"受访者 {subject_ref} 授权到期")
        )
        return events

    # ------------------------------------------------------------------ 转写

    def add_transcript_version(
        self,
        transcript_id: str,
        source_asset_id: str,
        text: str,
        editor_ref: str | None = None,
        narrative_id: str | None = None,
        at: str | None = None,
    ) -> Event:
        at = at or now_iso()
        with self._lock:
            aggregate_id = f"transcript:{transcript_id}"
            version_no = self.store.version(aggregate_id) + 1
            payload: dict[str, Any] = {
                "transcript_id": transcript_id,
                "source_asset_id": f"asset:{source_asset_id}",
                "version_no": version_no,
                "text": text,
            }
            if editor_ref:
                payload["editor_ref"] = editor_ref
            if narrative_id:
                payload["narrative_id"] = narrative_id
            return self._append("TRANSCRIPT_VERSIONED", "transcript", aggregate_id, payload, at)

    # ------------------------------------------------------------------ 复核

    def open_review(
        self,
        review_case_id: str,
        target_ref: str,
        *,
        kind: str = "expert",
        cited_boundary_ref: str | None = None,
        cited_revision_no: int | None = None,
        note: str | None = None,
        at: str | None = None,
    ) -> Event:
        """开启复核。空间判断类复核可携带素材采集时引用的边界修订号。"""
        at = at or now_iso()
        with self._lock:
            payload: dict[str, Any] = {"review_case_id": review_case_id, "target_ref": target_ref, "kind": kind}
            current_revision_no = None
            if cited_boundary_ref is not None:
                payload["cited_boundary_ref"] = cited_boundary_ref
                payload["cited_revision_no"] = cited_revision_no
                revisions = self.state["boundaries"].get(f"boundary:{cited_boundary_ref}", [])
                if revisions:
                    current_revision_no = max(b["revision_no"] for b in revisions)
                    payload["current_revision_no"] = current_revision_no
            if note:
                payload["note"] = note
            return self._append("REVIEW_OPENED", "review_case", f"review:{review_case_id}", payload, at)

    def return_review(self, review_case_id: str, reason: str, at: str | None = None) -> Event:
        """专家退回：只记录退回意见与轮次，不产生任何改写原始资料的事件。"""
        at = at or now_iso()
        with self._lock:
            aggregate_id = f"review:{review_case_id}"
            review = self.state["reviews"].get(aggregate_id)
            if review is None:
                raise ReviewError(f"复核案件 {review_case_id} 不存在")
            if review["status"] == "completed":
                raise ReviewError(f"复核案件 {review_case_id} 已结案，不能再退回")
            return self._append(
                "REVIEW_RETURNED",
                "review_case",
                aggregate_id,
                {"review_case_id": review_case_id, "reason": reason},
                at,
            )

    def complete_review(self, review_case_id: str, decision: str, at: str | None = None) -> Event:
        at = at or now_iso()
        with self._lock:
            if decision not in ("approved", "rejected"):
                raise ReviewError("复核结论必须是 approved 或 rejected")
            aggregate_id = f"review:{review_case_id}"
            review = self.state["reviews"].get(aggregate_id)
            if review is None:
                raise ReviewError(f"复核案件 {review_case_id} 不存在")
            if review["status"] == "completed":
                raise ReviewError(f"复核案件 {review_case_id} 已结案")
            return self._append(
                "REVIEW_COMPLETED",
                "review_case",
                aggregate_id,
                {"review_case_id": review_case_id, "decision": decision},
                at,
            )

    def pending_reviews(self) -> list[dict[str, Any]]:
        """待复核/被退回待补正的案件，服务重启后仍可继续处理。"""
        with self._lock:
            return [
                {"review_case_id": r["review_case_id"], "target_ref": r["target_ref"], "kind": r["kind"], "status": r["status"]}
                for r in self.state["reviews"].values()
                if r["status"] in ("open", "returned")
            ]

    # ------------------------------------------------------------------ 快照

    def _independent_review_approved(self, target_ref: str, moment: datetime) -> bool:
        for review in self.state["reviews"].values():
            if (
                review["target_ref"] == target_ref
                and review["kind"] == "independent"
                and review["status"] == "completed"
                and review["decision"] == "approved"
                and parse_ts(review["completed_at"]) <= moment
            ):
                return True
        return False

    def publish_snapshot(
        self, snapshot_id: str, cutoff_at: str, visibility: str = "public", at: str | None = None
    ) -> Event:
        """发布截止某时刻的档案快照；未成年人材料缺少独立复核通过则不进入公开快照。"""
        at = at or now_iso()
        cutoff = parse_ts(cutoff_at)
        with self._lock:
            if f"snapshot:{snapshot_id}" in self.state["snapshots"]:
                raise ConflictError(f"快照 {snapshot_id} 已存在")
            entries: list[dict[str, Any]] = []
            for asset_id, asset in self.state["assets"].items():
                if parse_ts(asset["uploaded_at"]) > cutoff:
                    continue
                subject = asset.get("subject_ref")
                record = self.state["consents"].get(f"consent:{subject}") if subject else None
                scopes = self._scopes_at(record, cutoff) if record else set()
                if subject:
                    eligible_scopes = (
                        {asset["consent_scope"]}
                        if asset["consent_scope"] in PUBLIC_SCOPES and asset["consent_scope"] in scopes
                        else set()
                    )
                else:
                    # 无受访者素材（如地点照片）：以上传时登记的授权范围为准
                    eligible_scopes = {asset["consent_scope"]} if asset["consent_scope"] in PUBLIC_SCOPES else set()
                if not eligible_scopes:
                    continue
                minor_blocked = (
                    record
                    and record.get("is_minor")
                    and not self._independent_review_approved(asset_id, cutoff)
                )
                base = {
                    "asset_ref": asset_id,
                    "kind": asset["kind"],
                    "place_ref": asset.get("place_ref"),
                    "subject_ref": subject,
                    "summary": asset.get("summary"),
                    "boundary_ref": asset.get("boundary_ref"),
                    "boundary_revision": asset.get("boundary_revision"),
                }
                # 未成年人材料缺独立复核：公开版本不入快照（内部研究的最小事实仍在素材与审计视图中保留）
                for scope in sorted(eligible_scopes):
                    if minor_blocked:
                        continue
                    entries.append({**base, "ref": f"{asset_id}#{scope}", "scope": scope})
            return self._append(
                "SNAPSHOT_PUBLISHED",
                "archive_snapshot",
                f"snapshot:{snapshot_id}",
                {
                    "snapshot_id": snapshot_id,
                    "cutoff_at": cutoff_at,
                    "visibility": visibility,
                    "entries": entries,
                },
                at,
            )

    # ---------------------------------------------------------- 到期与恢复

    def run_due(self, now: str | None = None) -> dict[str, Any]:
        """服务恢复后继续推进：处理授权/资质到期，并列待复核任务。

        到期只追加事件并定向冻结公开版本；内部研究事实不动。
        """
        now = now or now_iso()
        moment = parse_ts(now)
        report: dict[str, Any] = {
            "expired_consents": [],
            "expired_certifications": [],
            "frozen_entries": 0,
            "pending_reviews": self.pending_reviews(),
        }
        with self._lock:
            for consent_id, record in list(self.state["consents"].items()):
                processed_through = record.get("expired_at")
                due_grants = [
                    g
                    for g in record["grants"]
                    if g.get("valid_until")
                    and parse_ts(g["valid_until"]) <= moment
                    and (not processed_through or parse_ts(g["valid_until"]) > parse_ts(processed_through))
                ]
                if due_grants:
                    subject_ref = consent_id.split("consent:", 1)[1]
                    expired_at = min(g["valid_until"] for g in due_grants)
                    before = sum(len(self._frozen_entry_refs(s)) for s in self.state["snapshots"].values())
                    self._expire_consent(subject_ref, expired_at)
                    after = sum(len(self._frozen_entry_refs(s)) for s in self.state["snapshots"].values())
                    report["expired_consents"].append(subject_ref)
                    report["frozen_entries"] += after - before
            for cert_id, cert in list(self.state["certs"].items()):
                if cert["status"] == "valid" and cert.get("valid_until") and moment >= parse_ts(cert["valid_until"]):
                    self._append(
                        "CERTIFICATION_EXPIRED",
                        "collector_profile",
                        cert_id,
                        {"certification_id": cert["certification_id"], "expired_at": cert["valid_until"]},
                        now,
                    )
                    report["expired_certifications"].append(cert["certification_id"])
        return report

    # ------------------------------------------------------------ 公众档案

    @staticmethod
    def _pseudonym(subject_ref: str) -> str:
        digest = hashlib.sha256(subject_ref.encode("utf-8")).hexdigest()[:8]
        return f"受访者-{digest}"

    def public_catalog(self, at: str | None = None) -> dict[str, Any]:
        """面向公众的脱敏档案。

        - 只含已发布、未冻结且当前授权仍覆盖的公开方面（声音/转写/肖像）；
        - 姓名等身份信息仅在 identity_public 仍有效时展示，否则以稳定假名替代
          （授权公开声音不等于授权公开姓名）。
        """
        moment = parse_ts(at) if at else parse_ts(now_iso())
        with self._lock:
            places_out: dict[str, dict[str, Any]] = {}
            merged: dict[str, dict[str, Any]] = {}
            for snapshot in self.state["snapshots"].values():
                if parse_ts(snapshot["published_at"]) > moment:
                    continue
                frozen = self._frozen_entry_refs(snapshot, moment)
                for entry in snapshot["entries"]:
                    if entry["ref"] in frozen:
                        continue
                    scope = entry.get("scope")
                    if scope not in PUBLIC_SCOPES:
                        continue
                    subject = entry.get("subject_ref")
                    consent = self.state["consents"].get(f"consent:{subject}") if subject else None
                    if consent:
                        live = self._scopes_at(consent, moment)
                    elif subject:
                        live = set()
                    else:
                        live = {scope}  # 无受访者素材以上传登记为准
                    if scope not in live:
                        continue
                    place_id = entry.get("place_ref")
                    if place_id and place_id not in places_out:
                        place = self.state["places"].get(f"place:{place_id}")
                        aliases = (
                            sorted(
                                n["name"]
                                for n in place["names"]
                                if parse_ts(n["valid_from"]) <= moment
                                and (not n.get("valid_until") or moment < parse_ts(n["valid_until"]))
                            )
                            if place
                            else []
                        )
                        places_out[place_id] = {"place_id": place_id, "aliases": aliases}
                    record = merged.setdefault(
                        entry["asset_ref"],
                        {
                            "ref": entry["asset_ref"],
                            "kind": entry["kind"],
                            "place_ref": place_id,
                            "subject_ref": subject,
                            "summary": entry.get("summary"),
                            "aspects": set(),
                        },
                    )
                    record["aspects"].add(scope)
            records: list[dict[str, Any]] = []
            for record in merged.values():
                subject = record.pop("subject_ref")
                consent = self.state["consents"].get(f"consent:{subject}") if subject else None
                identity_ok = bool(consent and "identity_public" in self._scopes_at(consent, moment))
                records.append(
                    {
                        **record,
                        "aspects": sorted(record["aspects"]),
                        "subject": subject if identity_ok else (self._pseudonym(subject) if subject else None),
                        "identified": identity_ok,
                    }
                )
            records.sort(key=lambda r: r["ref"])
            return {"as_of": moment.isoformat(), "places": sorted(places_out.values(), key=lambda x: x["place_id"]), "records": records}

    # ------------------------------------------------------------ 审计重建

    def rebuild_as_of(self, as_of: str) -> dict[str, Any]:
        """研究人员审计命令：按历史日期重建地点称谓、证据来源、冲突观点与当时公开范围。"""
        moment = parse_ts(as_of)
        state = _new_state()
        for event in self.store.load_all(until=moment):
            apply(state, event)

        places_out: dict[str, Any] = {}
        for place_id, place in state["places"].items():
            names = [
                {
                    "name": n["name"],
                    "valid_from": n["valid_from"],
                    "valid_until": n.get("valid_until"),
                    "basis_event": n["event_id"],
                    "in_use": parse_ts(n["valid_from"]) <= moment
                    and (not n.get("valid_until") or moment < parse_ts(n["valid_until"])),
                }
                for n in place["names"]
            ]
            narratives = []
            for narrative in place["narratives"]:
                if parse_ts(narrative["captured_at"]) > moment:
                    continue
                evidence: dict[str, Any] = {"narrative_event": narrative["event_id"]}
                if narrative.get("asset_ref"):
                    asset = state["assets"].get(f"asset:{narrative['asset_ref']}")
                    if asset:
                        evidence["source_asset"] = {
                            "asset_id": narrative["asset_ref"],
                            "content_hash": asset["content_hash"],
                            "boundary_ref": asset.get("boundary_ref"),
                            "boundary_revision": asset.get("boundary_revision"),
                        }
                transcript_versions = []
                for transcript_id, transcript in state["transcripts"].items():
                    for version_no, version in sorted(transcript["versions"].items()):
                        if version.get("narrative_id") == narrative["narrative_id"]:
                            transcript_versions.append(
                                {"transcript_id": transcript_id.split("transcript:", 1)[1], "version_no": version_no, "basis_event": version["event_id"]}
                            )
                if transcript_versions:
                    evidence["transcript_versions"] = transcript_versions
                narratives.append(
                    {
                        "narrative_id": narrative["narrative_id"],
                        "generation": narrative["generation"],
                        "captured_at": narrative["captured_at"],
                        "summary": narrative.get("summary"),
                        "evidence": evidence,
                    }
                )
            places_out[place_id.split("place:", 1)[1]] = {
                "names": sorted(names, key=lambda n: n["valid_from"]),
                "merged_into": place["merged_into"],
                "narratives": narratives,
                "conflicting_views": [
                    {"generation": n["generation"], "summary": n.get("summary"), "narrative_id": n["narrative_id"]}
                    for n in narratives
                ],
            }

        consents_out = {}
        for consent_id, record in state["consents"].items():
            subject = consent_id.split("consent:", 1)[1]
            consents_out[subject] = {
                "scopes_available": sorted(self._scopes_at(record, moment)),
                "is_minor": record.get("is_minor", False),
                "guardian_subject_ref": record.get("guardian_subject_ref"),
                "basis": [
                    {"scope": g["scope"], "granted_at": g["at"], "valid_until": g.get("valid_until"), "event_id": g["event_id"]}
                    for g in record["grants"]
                ],
                "narrowed": [{"scope": n["scope"], "at": n["at"], "event_id": n["event_id"]} for n in record["narrowed"]],
                "expired_at": record.get("expired_at"),
            }

        boundaries_out = {}
        for boundary_id, revisions in state["boundaries"].items():
            effective = [r for r in revisions if parse_ts(r["effective_at"]) <= moment]
            if effective:
                current = max(effective, key=lambda r: r["revision_no"])
                boundaries_out[boundary_id.split("boundary:", 1)[1]] = {
                    "current_revision_no": current["revision_no"],
                    "history": [
                        {"revision_no": r["revision_no"], "effective_at": r["effective_at"], "event_id": r["event_id"]}
                        for r in sorted(revisions, key=lambda r: r["revision_no"])
                    ],
                }

        snapshots_out = []
        for snapshot_id, snapshot in state["snapshots"].items():
            if parse_ts(snapshot["published_at"]) > moment:
                continue
            frozen = self._frozen_entry_refs(snapshot, moment)
            snapshots_out.append(
                {
                    "snapshot_id": snapshot["snapshot_id"],
                    "cutoff_at": snapshot["cutoff_at"],
                    "published_at": snapshot["published_at"],
                    "available_entries": [e["ref"] for e in snapshot["entries"] if e["ref"] not in frozen],
                    "frozen_entries": sorted(frozen),
                }
            )

        return {
            "as_of": moment.isoformat(),
            "places": places_out,
            "boundaries": boundaries_out,
            "consents": consents_out,
            "public_range": {"snapshots": snapshots_out},
        }
