"""青年遗产田野资料治理服务。

在基础交换契约之上实现业务幂等、冲突隔离与状态推进：

- 采集任务认领：校验培训资质有效期，并发认领只有一人获得提交责任；
- 原始文件摘要上传：可乱序重试，相同业务键只有文件指纹和授权范围一致
  才视为幂等，否则冲突隔离记录，不改写已有资料；
- 地点沿革与合并：保留旧称与有效期，不同代际叙述各自留存，绝不覆盖
  成一个答案；
- 受访者同意：收窄许可时只冻结依赖其身份或声音的公开版本，已用于内部
  研究的最小事实按原依据保留；授权到期由恢复流程继续处理；
- 未成年人材料：公开前必须具备监护授权与独立复核；
- 专家复核：退回只改变状态，不改写原始资料；
- 数字档案快照：发布前校验复核、授权与截止时间，公众视图始终脱敏。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Iterable, Mapping, Sequence
from uuid import uuid4

from .store import EventStore

CONSENT_SCOPES = ("internal_research", "name_public", "voice_public", "image_public")
MEDIA_SCOPE_REQUIREMENTS = {
    "audio": ("voice_public",),
    "video": ("voice_public", "image_public"),
    "photo": ("image_public",),
    "text": (),
}
REVIEW_OUTCOMES = ("approved", "returned")
REVIEW_KINDS = ("expert", "independent")
VISIBILITIES = ("public", "internal")


class ServiceError(Exception):
    """治理服务业务错误。"""


class AssignmentError(ServiceError):
    """采集任务登记、认领或提交责任错误。"""


class ClaimConflictError(AssignmentError):
    """采集任务已被他人认领。"""


class QualificationError(AssignmentError):
    """缺少有效培训资质。"""


class SubmissionResponsibilityError(ServiceError):
    """上传人不持有该采集任务的提交责任。"""


class UploadConflictError(ServiceError):
    """相同业务键但文件指纹或授权范围不一致。"""

    def __init__(self, message: str, conflict_event: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.conflict_event = conflict_event


class TranscriptionError(ServiceError):
    """转写版本错误。"""


class PlaceError(ServiceError):
    """地点沿革错误。"""


class ConsentError(ServiceError):
    """受访者同意错误。"""


class ReviewError(ServiceError):
    """专家复核错误。"""


class PublishError(ServiceError):
    """快照发布错误。"""


def parse_ts(value: str, field: str = "occurred_at") -> datetime:
    """解析带时区的 ISO 时间；缺时区视为契约违规。"""
    if not isinstance(value, str):
        raise ServiceError(f"{field} 必须是带时区的 ISO 时间字符串")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ServiceError(f"{field} 不是合法的 ISO 时间: {value!r}") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ServiceError(f"{field} 必须携带时区: {value!r}")
    return parsed


def new_state() -> dict[str, Any]:
    """空投影状态。"""
    return {
        "qualifications": {},
        "assignments": {},
        "assets": {},
        "transcriptions": {},
        "places": {},
        "consents": {},
        "snapshots": {},
        "reviews": [],
        "freezes": [],
        "conflicts": [],
        "versions": {},
    }


def _new_place() -> dict[str, Any]:
    return {"names": [], "merged_into": None, "narratives": [], "judgments": []}


def _new_consent() -> dict[str, Any]:
    return {
        "scope": set(),
        "valid_until": None,
        "is_minor": False,
        "guardian": None,
        "guardian_scope": set(),
    }


def apply_event(state: dict[str, Any], event: Mapping[str, Any]) -> None:
    """把一条已存事件投影到状态；投影只追加事实，不改写历史。"""
    event_type = event["event_type"]
    payload = event["payload"]
    occurred_at = event["occurred_at"]
    version_key = (event["aggregate_type"], event["aggregate_id"])
    state["versions"][version_key] = max(event["version"], state["versions"].get(version_key, 0))

    if event_type == "QUALIFICATION_RECORDED":
        state["qualifications"][(payload["student_ref"], payload["skill"])] = {
            "valid_from": payload["valid_from"],
            "valid_until": payload["valid_until"],
            "recorded_at": occurred_at,
        }
    elif event_type == "ASSIGNMENT_REGISTERED":
        state["assignments"][event["aggregate_id"]] = {
            "required_skill": payload["required_skill"],
            "claimed_by": None,
            "claimed_at": None,
            "status": "open",
        }
    elif event_type == "ASSIGNMENT_CLAIMED":
        assignment = state["assignments"].setdefault(
            event["aggregate_id"],
            {"required_skill": None, "claimed_by": None, "claimed_at": None, "status": "open"},
        )
        assignment.update(
            {"claimed_by": payload["student_ref"], "claimed_at": occurred_at, "status": "claimed"}
        )
    elif event_type == "ASSIGNMENT_RELEASED":
        assignment = state["assignments"].get(event["aggregate_id"])
        if assignment is not None:
            assignment.update({"claimed_by": None, "claimed_at": None, "status": "open"})
    elif event_type == "ASSET_UPLOADED":
        state["assets"][payload["business_key"]] = {
            "business_key": payload["business_key"],
            "content_hash": payload["content_hash"],
            "captured_at": payload["captured_at"],
            "uploader_ref": payload["uploader_ref"],
            "assignment_ref": payload["assignment_ref"],
            "authorized_scope": sorted(payload.get("authorized_scope", [])),
            "media_type": payload["media_type"],
            "subjects": sorted(payload.get("subject_refs", [])),
            "place_ref": payload.get("place_ref"),
            "status": "submitted",
            "occurred_at": occurred_at,
        }
    elif event_type == "ASSET_UPLOAD_CONFLICTED":
        state["conflicts"].append({**payload, "occurred_at": occurred_at})
    elif event_type == "TRANSCRIPTION_COMMITTED":
        state["transcriptions"].setdefault(payload["asset_ref"], []).append(
            {
                "transcription_version": payload["transcription_version"],
                "text_hash": payload["text_hash"],
                "author_ref": payload["author_ref"],
                "occurred_at": occurred_at,
            }
        )
    elif event_type == "PLACE_REGISTERED":
        state["places"][event["aggregate_id"]] = {
            **_new_place(),
            "names": [
                {
                    "name": payload["name"],
                    "valid_from": payload["valid_from"],
                    "valid_to": payload.get("valid_to"),
                    "recorded_at": occurred_at,
                }
            ],
        }
    elif event_type == "PLACE_NAME_RECORDED":
        place = state["places"].setdefault(event["aggregate_id"], _new_place())
        place["names"].append(
            {
                "name": payload["name"],
                "valid_from": payload["valid_from"],
                "valid_to": payload.get("valid_to"),
                "recorded_at": occurred_at,
            }
        )
    elif event_type == "PLACE_MERGED":
        merged = state["places"].setdefault(payload["merged_place"], _new_place())
        merged["merged_into"] = payload["surviving_place"]
    elif event_type == "NARRATIVE_RECORDED":
        place = state["places"].setdefault(payload["place_ref"], _new_place())
        place["narratives"].append(
            {
                "narrator_ref": payload["narrator_ref"],
                "cohort": payload["cohort"],
                "summary_hash": payload["summary_hash"],
                "occurred_at": occurred_at,
            }
        )
    elif event_type == "SPATIAL_JUDGMENT_RECORDED":
        place = state["places"].setdefault(payload["place_ref"], _new_place())
        place["judgments"].append(
            {
                "judgment_id": event["aggregate_id"],
                "boundary_version": payload["boundary_version"],
                "judgment": payload["judgment"],
                "author_ref": payload["author_ref"],
                "occurred_at": occurred_at,
            }
        )
    elif event_type in ("CONSENT_UPDATED", "CONSENT_EXPIRED"):
        consent = state["consents"].setdefault(payload["subject_ref"], _new_consent())
        consent["scope"] = set(payload["scope"])
        if payload.get("valid_until") is not None:
            consent["valid_until"] = payload["valid_until"]
        if payload.get("is_minor") is not None:
            consent["is_minor"] = bool(payload["is_minor"])
    elif event_type == "GUARDIAN_AUTHORIZATION_RECORDED":
        consent = state["consents"].setdefault(payload["subject_ref"], _new_consent())
        consent.update(
            {
                "guardian": payload["guardian_ref"],
                "guardian_scope": set(payload["scope"]),
                "is_minor": True,
            }
        )
    elif event_type == "REVIEW_COMPLETED":
        state["reviews"].append(
            {
                "target_ref": payload["target_ref"],
                "outcome": payload["outcome"],
                "reviewer_ref": payload["reviewer_ref"],
                "review_kind": payload["review_kind"],
                "occurred_at": occurred_at,
            }
        )
        asset = state["assets"].get(payload["target_ref"])
        if asset is not None and payload["review_kind"] == "expert":
            asset["status"] = "approved" if payload["outcome"] == "approved" else "returned"
    elif event_type == "PUBLIC_VERSION_FROZEN":
        state["freezes"].append(
            {
                "snapshot_ref": payload["snapshot_ref"],
                "subject_ref": payload["subject_ref"],
                "revoked_scopes": sorted(payload["revoked_scopes"]),
                "reason": payload["reason"],
                "occurred_at": occurred_at,
            }
        )
        snapshot = state["snapshots"].get(payload["snapshot_ref"])
        if snapshot is not None:
            snapshot["frozen"] = True
            snapshot["freeze_reason"] = payload["reason"]
    elif event_type == "SNAPSHOT_PUBLISHED":
        state["snapshots"][event["aggregate_id"]] = {
            "snapshot": event["aggregate_id"],
            "cutoff_at": payload["cutoff_at"],
            "visibility": payload["visibility"],
            "asset_refs": list(payload["asset_refs"]),
            "named_subjects": sorted(payload.get("named_subjects", [])),
            "purpose": payload.get("purpose"),
            "occurred_at": occurred_at,
            "frozen": False,
            "freeze_reason": None,
        }


def resolve_place_members(state: Mapping[str, Any], place_id: str) -> list[str]:
    """沿合并谱系找到相关地点：被查询地点、存续地点及所有并入来源。"""
    places = state["places"]
    members: list[str] = []
    seen: set[str] = set()
    current = place_id
    while current and current not in seen and current in places:
        seen.add(current)
        members.append(current)
        current = places[current].get("merged_into")
    changed = True
    while changed:
        changed = False
        for pid, place in places.items():
            if pid not in seen and place.get("merged_into") in seen:
                seen.add(pid)
                members.append(pid)
                changed = True
    return members


def surviving_place(state: Mapping[str, Any], place_id: str) -> str:
    """返回谱系中未被合并的存续地点。"""
    for pid in resolve_place_members(state, place_id):
        if state["places"][pid].get("merged_into") is None:
            return pid
    return place_id


def active_place_names(state: Mapping[str, Any], place_id: str, at: str) -> list[dict[str, Any]]:
    """指定地点在某一时刻有效的称谓（含并入来源的旧称）。"""
    at_dt = parse_ts(at, "at")
    names: list[dict[str, Any]] = []
    for pid in resolve_place_members(state, place_id):
        for entry in state["places"][pid]["names"]:
            valid_from = parse_ts(entry["valid_from"], "valid_from")
            valid_to = entry.get("valid_to")
            if valid_from <= at_dt and (valid_to is None or at_dt <= parse_ts(valid_to, "valid_to")):
                names.append(
                    {
                        "name": entry["name"],
                        "valid_from": entry["valid_from"],
                        "valid_to": valid_to,
                        "source_place": pid,
                    }
                )
    names.sort(key=lambda item: (item["valid_from"], item["name"]))
    return names


class GovernanceService:
    """田野资料治理服务：命令校验、事件追加与状态投影。"""

    def __init__(self, store: EventStore):
        self.store = store
        self.state = new_state()
        self._replay()

    def _replay(self) -> None:
        self.state = new_state()
        for event in self.store.events():
            apply_event(self.state, event)

    # --- 内部工具 -----------------------------------------------------

    def _append(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        occurred_at: str,
        payload: Mapping[str, Any],
        event_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        parse_ts(occurred_at)
        version_key = (aggregate_type, aggregate_id)
        version = self.state["versions"].get(version_key, 0) + 1
        event = {
            "event_id": event_id or f"{aggregate_type}:{aggregate_id}:v{version}:{uuid4().hex[:8]}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": occurred_at,
            "version": version,
            "payload": dict(payload),
        }
        stored, appended = self.store.append(event)
        if appended:
            apply_event(self.state, stored)
        return stored, appended

    def _find_event(
        self,
        event_type: str,
        aggregate_id: str,
        predicate: Callable[[Mapping[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        for event in self.store.events():
            if event["event_type"] != event_type or event["aggregate_id"] != aggregate_id:
                continue
            if predicate is not None and not predicate(event):
                continue
            return event
        raise ServiceError(f"状态与日志不一致：找不到 {event_type}/{aggregate_id} 的原始事件")

    @staticmethod
    def _validate_scope(scope: Iterable[str]) -> set[str]:
        values = set(scope)
        unknown = values - set(CONSENT_SCOPES)
        if unknown:
            raise ConsentError(f"授权范围包含未登记值: {sorted(unknown)}")
        return values

    # --- 培训资质 -----------------------------------------------------

    def record_qualification(
        self,
        student_ref: str,
        skill: str,
        valid_from: str,
        valid_until: str,
        occurred_at: str,
        *,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        parse_ts(valid_from, "valid_from")
        parse_ts(valid_until, "valid_until")
        payload = {
            "student_ref": student_ref,
            "skill": skill,
            "valid_from": valid_from,
            "valid_until": valid_until,
        }
        event, _ = self._append(
            "QUALIFICATION_RECORDED",
            "training_qualification",
            f"{student_ref}:{skill}",
            occurred_at,
            payload,
            event_id=event_id,
        )
        return event

    # --- 采集任务 -----------------------------------------------------

    def register_assignment(
        self,
        assignment_id: str,
        required_skill: str,
        occurred_at: str,
        *,
        title: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        if assignment_id in self.state["assignments"]:
            raise AssignmentError(f"采集任务已登记: {assignment_id}")
        payload: dict[str, Any] = {"required_skill": required_skill}
        if title:
            payload["title"] = title
        event, _ = self._append(
            "ASSIGNMENT_REGISTERED", "field_assignment", assignment_id, occurred_at, payload, event_id=event_id
        )
        return event

    def claim_assignment(
        self,
        assignment_id: str,
        student_ref: str,
        occurred_at: str,
        *,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        at_dt = parse_ts(occurred_at)
        assignment = self.state["assignments"].get(assignment_id)
        if assignment is None:
            raise AssignmentError(f"采集任务不存在: {assignment_id}")
        claimed_by = assignment.get("claimed_by")
        if claimed_by == student_ref:
            return self._find_event("ASSIGNMENT_CLAIMED", assignment_id)
        if claimed_by:
            raise ClaimConflictError(
                f"采集任务 {assignment_id} 已由 {claimed_by} 认领，提交责任唯一"
            )
        self._require_qualification(student_ref, assignment["required_skill"], at_dt)
        event, _ = self._append(
            "ASSIGNMENT_CLAIMED",
            "field_assignment",
            assignment_id,
            occurred_at,
            {"student_ref": student_ref},
            event_id=event_id,
        )
        return event

    def _require_qualification(self, student_ref: str, skill: str, at_dt: datetime) -> None:
        qualification = self.state["qualifications"].get((student_ref, skill))
        if qualification is None:
            raise QualificationError(f"{student_ref} 缺少 {skill} 培训资质")
        valid_from = parse_ts(qualification["valid_from"], "valid_from")
        valid_until = parse_ts(qualification["valid_until"], "valid_until")
        if not valid_from <= at_dt <= valid_until:
            raise QualificationError(
                f"{student_ref} 的 {skill} 培训资质在认领时不在有效期内"
            )

    def release_assignment(
        self,
        assignment_id: str,
        student_ref: str,
        occurred_at: str,
        *,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        assignment = self.state["assignments"].get(assignment_id)
        if assignment is None:
            raise AssignmentError(f"采集任务不存在: {assignment_id}")
        if assignment.get("claimed_by") != student_ref:
            raise AssignmentError("只有当前认领人可以放弃认领")
        event, _ = self._append(
            "ASSIGNMENT_RELEASED",
            "field_assignment",
            assignment_id,
            occurred_at,
            {"student_ref": student_ref},
            event_id=event_id,
        )
        return event

    # --- 原始文件摘要 -------------------------------------------------

    def upload_asset(
        self,
        *,
        business_key: str,
        content_hash: str,
        captured_at: str,
        uploader_ref: str,
        assignment_ref: str,
        authorized_scope: Iterable[str],
        media_type: str,
        occurred_at: str,
        subject_refs: Iterable[str] = (),
        place_ref: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """上传原始文件摘要。

        相同业务键只有文件指纹和授权范围一致才视为幂等；不一致时追加
        冲突隔离事件并抛出 UploadConflictError，已有资料保持不变。
        """
        parse_ts(captured_at, "captured_at")
        scope = self._validate_scope(authorized_scope)
        if media_type not in MEDIA_SCOPE_REQUIREMENTS:
            raise ServiceError(f"未登记的媒体类型: {media_type}")
        assignment = self.state["assignments"].get(assignment_ref)
        if assignment is None or assignment.get("claimed_by") != uploader_ref:
            raise SubmissionResponsibilityError(
                f"采集任务 {assignment_ref} 的提交责任不属于 {uploader_ref}"
            )
        existing = self.state["assets"].get(business_key)
        if existing is not None:
            if existing["content_hash"] == content_hash and existing["authorized_scope"] == sorted(scope):
                return self._find_event("ASSET_UPLOADED", business_key)
            conflict_payload = {
                "business_key": business_key,
                "content_hash": content_hash,
                "existing_hash": existing["content_hash"],
                "attempted_scope": sorted(scope),
                "existing_scope": existing["authorized_scope"],
                "uploader_ref": uploader_ref,
                "reason": "fingerprint_or_scope_mismatch",
            }
            conflict_event, _ = self._append(
                "ASSET_UPLOAD_CONFLICTED", "source_asset", business_key, occurred_at, conflict_payload
            )
            raise UploadConflictError(
                f"业务键 {business_key} 已存在：文件指纹或授权范围不一致，冲突已隔离记录",
                conflict_event,
            )
        payload: dict[str, Any] = {
            "business_key": business_key,
            "content_hash": content_hash,
            "captured_at": captured_at,
            "uploader_ref": uploader_ref,
            "assignment_ref": assignment_ref,
            "authorized_scope": sorted(scope),
            "media_type": media_type,
            "subject_refs": sorted(set(subject_refs)),
        }
        if place_ref is not None:
            payload["place_ref"] = place_ref
        event, _ = self._append(
            "ASSET_UPLOADED", "source_asset", business_key, occurred_at, payload, event_id=event_id
        )
        return event

    # --- 转写版本 -----------------------------------------------------

    def commit_transcription(
        self,
        asset_ref: str,
        transcription_version: int,
        text_hash: str,
        author_ref: str,
        occurred_at: str,
        *,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        if asset_ref not in self.state["assets"]:
            raise TranscriptionError(f"原始资料不存在: {asset_ref}")
        if (
            isinstance(transcription_version, bool)
            or not isinstance(transcription_version, int)
            or transcription_version < 1
        ):
            raise TranscriptionError("转写版本必须是从 1 开始的正整数")
        versions = self.state["transcriptions"].get(asset_ref, [])
        for existing in versions:
            if existing["transcription_version"] == transcription_version:
                if existing["text_hash"] == text_hash:
                    return self._find_event(
                        "TRANSCRIPTION_COMMITTED",
                        asset_ref,
                        lambda e: e["payload"].get("transcription_version") == transcription_version,
                    )
                raise TranscriptionError(f"转写版本 {transcription_version} 已存在，不允许覆盖")
        expected = max((v["transcription_version"] for v in versions), default=0) + 1
        if transcription_version != expected:
            raise TranscriptionError(f"转写版本必须递增，下一个版本应为 {expected}")
        payload = {
            "asset_ref": asset_ref,
            "transcription_version": transcription_version,
            "text_hash": text_hash,
            "author_ref": author_ref,
        }
        event, _ = self._append(
            "TRANSCRIPTION_COMMITTED", "transcription", asset_ref, occurred_at, payload, event_id=event_id
        )
        return event

    # --- 地点沿革 -----------------------------------------------------

    def register_place(
        self,
        place_id: str,
        name: str,
        valid_from: str,
        occurred_at: str,
        *,
        valid_to: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        parse_ts(valid_from, "valid_from")
        if valid_to is not None:
            parse_ts(valid_to, "valid_to")
        if place_id in self.state["places"]:
            raise PlaceError(f"地点已登记: {place_id}")
        payload: dict[str, Any] = {"name": name, "valid_from": valid_from}
        if valid_to is not None:
            payload["valid_to"] = valid_to
        event, _ = self._append(
            "PLACE_REGISTERED", "place_record", place_id, occurred_at, payload, event_id=event_id
        )
        return event

    def record_place_name(
        self,
        place_id: str,
        name: str,
        valid_from: str,
        occurred_at: str,
        *,
        valid_to: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        parse_ts(valid_from, "valid_from")
        if valid_to is not None:
            parse_ts(valid_to, "valid_to")
        place = self.state["places"].get(place_id)
        if place is None:
            raise PlaceError(f"地点不存在: {place_id}")
        if place.get("merged_into"):
            raise PlaceError(
                f"地点 {place_id} 已并入 {place['merged_into']}，请在存续地点上登记名称"
            )
        payload = {"name": name, "valid_from": valid_from}
        if valid_to is not None:
            payload["valid_to"] = valid_to
        event, _ = self._append(
            "PLACE_NAME_RECORDED", "place_record", place_id, occurred_at, payload, event_id=event_id
        )
        return event

    def merge_places(
        self,
        surviving: str,
        merged: str,
        occurred_at: str,
        *,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """合并地点：旧称与有效期全部保留，代际叙述各自留存。"""
        places = self.state["places"]
        if surviving == merged:
            raise PlaceError("存续地点与被合并地点不能相同")
        if surviving not in places:
            raise PlaceError(f"存续地点不存在: {surviving}")
        if merged not in places:
            raise PlaceError(f"被合并地点不存在: {merged}")
        if places[surviving].get("merged_into"):
            raise PlaceError(f"存续地点 {surviving} 已并入其他地点")
        current = places[merged].get("merged_into")
        if current == surviving:
            return self._find_event(
                "PLACE_MERGED", surviving, lambda e: e["payload"].get("merged_place") == merged
            )
        if current:
            raise PlaceError(f"地点 {merged} 已并入 {current}")
        payload = {"surviving_place": surviving, "merged_place": merged}
        event, _ = self._append(
            "PLACE_MERGED", "place_record", surviving, occurred_at, payload, event_id=event_id
        )
        return event

    def record_narrative(
        self,
        place_ref: str,
        narrator_ref: str,
        cohort: str,
        summary_hash: str,
        occurred_at: str,
        *,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        if place_ref not in self.state["places"]:
            raise PlaceError(f"地点不存在: {place_ref}")
        payload = {
            "place_ref": place_ref,
            "narrator_ref": narrator_ref,
            "cohort": cohort,
            "summary_hash": summary_hash,
        }
        event, _ = self._append(
            "NARRATIVE_RECORDED", "place_record", place_ref, occurred_at, payload, event_id=event_id
        )
        return event

    # --- 空间判断 -----------------------------------------------------

    def record_spatial_judgment(
        self,
        judgment_id: str,
        place_ref: str,
        boundary_version: str,
        judgment: str,
        author_ref: str,
        occurred_at: str,
        *,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        if place_ref not in self.state["places"]:
            raise PlaceError(f"地点不存在: {place_ref}")
        if ("spatial_judgment", judgment_id) in self.state["versions"]:
            raise PlaceError(f"空间判断已存在: {judgment_id}")
        payload = {
            "place_ref": place_ref,
            "boundary_version": boundary_version,
            "judgment": judgment,
            "author_ref": author_ref,
        }
        event, _ = self._append(
            "SPATIAL_JUDGMENT_RECORDED", "spatial_judgment", judgment_id, occurred_at, payload, event_id=event_id
        )
        return event

    # --- 受访者同意 ---------------------------------------------------

    def update_consent(
        self,
        subject_ref: str,
        scope: Iterable[str],
        occurred_at: str,
        *,
        valid_until: str | None = None,
        is_minor: bool | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """登记或收窄受访者许可。

        收窄时只冻结依赖其身份或声音的公开版本；内部研究已使用的最小
        事实按原依据保留。
        """
        new_scope = self._validate_scope(scope)
        if valid_until is not None:
            parse_ts(valid_until, "valid_until")
        previous = self.state["consents"].get(subject_ref)
        previous_scope = set(previous["scope"]) if previous else set()
        payload: dict[str, Any] = {"subject_ref": subject_ref, "scope": sorted(new_scope)}
        if valid_until is not None:
            payload["valid_until"] = valid_until
        if is_minor is not None:
            payload["is_minor"] = bool(is_minor)
        event, _ = self._append(
            "CONSENT_UPDATED", "consent_record", subject_ref, occurred_at, payload, event_id=event_id
        )
        revoked = previous_scope - new_scope
        self._freeze_dependent_public_versions(subject_ref, revoked, occurred_at, "consent_narrowed")
        return event

    def record_guardian_authorization(
        self,
        subject_ref: str,
        guardian_ref: str,
        scope: Iterable[str],
        occurred_at: str,
        *,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        guardian_scope = self._validate_scope(scope)
        payload = {
            "subject_ref": subject_ref,
            "guardian_ref": guardian_ref,
            "scope": sorted(guardian_scope),
        }
        event, _ = self._append(
            "GUARDIAN_AUTHORIZATION_RECORDED",
            "consent_record",
            subject_ref,
            occurred_at,
            payload,
            event_id=event_id,
        )
        return event

    def _snapshot_dependency(
        self, snapshot: Mapping[str, Any], subject_ref: str, revoked_scopes: set[str]
    ) -> set[str]:
        """公开版本对受访者被收回范围的依赖集合。"""
        deps: set[str] = set()
        if "name_public" in revoked_scopes and subject_ref in snapshot.get("named_subjects", []):
            deps.add("name_public")
        for key in snapshot["asset_refs"]:
            asset = self.state["assets"].get(key)
            if asset is None or subject_ref not in asset["subjects"]:
                continue
            for need in MEDIA_SCOPE_REQUIREMENTS.get(asset["media_type"], ()):
                if need in revoked_scopes:
                    deps.add(need)
        return deps

    def _freeze_dependent_public_versions(
        self, subject_ref: str, revoked_scopes: set[str], occurred_at: str, reason: str
    ) -> list[dict[str, Any]]:
        """冻结依赖被收回范围的公开版本；内部版本与原始资料不动。"""
        frozen_events: list[dict[str, Any]] = []
        if not revoked_scopes:
            return frozen_events
        snapshots = sorted(
            self.state["snapshots"].values(), key=lambda s: (s["occurred_at"], s["snapshot"])
        )
        for snapshot in snapshots:
            if snapshot["visibility"] != "public" or snapshot["frozen"]:
                continue
            deps = self._snapshot_dependency(snapshot, subject_ref, revoked_scopes)
            if not deps:
                continue
            payload = {
                "snapshot_ref": snapshot["snapshot"],
                "subject_ref": subject_ref,
                "revoked_scopes": sorted(deps),
                "reason": reason,
            }
            event, _ = self._append(
                "PUBLIC_VERSION_FROZEN", "public_release", snapshot["snapshot"], occurred_at, payload
            )
            frozen_events.append(event)
        return frozen_events

    # --- 专家复核 -----------------------------------------------------

    def complete_review(
        self,
        target_ref: str,
        outcome: str,
        reviewer_ref: str,
        occurred_at: str,
        *,
        review_kind: str = "expert",
        notes: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """完成复核。退回只改变状态，不改写原始资料。"""
        if outcome not in REVIEW_OUTCOMES:
            raise ReviewError(f"未登记的复核结论: {outcome}")
        if review_kind not in REVIEW_KINDS:
            raise ReviewError(f"未登记的复核类型: {review_kind}")
        asset = self.state["assets"].get(target_ref)
        if asset is None:
            raise ReviewError(f"复核对象不存在: {target_ref}")
        if review_kind == "expert" and asset["status"] != "submitted":
            raise ReviewError(
                f"资料 {target_ref} 当前状态为 {asset['status']}，不能重复专家复核；"
                "退回资料须以新业务键重新上传"
            )
        payload: dict[str, Any] = {
            "target_ref": target_ref,
            "outcome": outcome,
            "reviewer_ref": reviewer_ref,
            "review_kind": review_kind,
        }
        if notes:
            payload["notes"] = notes
        event, _ = self._append(
            "REVIEW_COMPLETED", "expert_review", target_ref, occurred_at, payload, event_id=event_id
        )
        return event

    # --- 数字档案快照 -------------------------------------------------

    def publish_snapshot(
        self,
        snapshot_id: str,
        cutoff_at: str,
        visibility: str,
        asset_refs: Iterable[str],
        occurred_at: str,
        *,
        purpose: str | None = None,
        named_subjects: Iterable[str] = (),
        event_id: str | None = None,
    ) -> dict[str, Any]:
        cutoff_dt = parse_ts(cutoff_at, "cutoff_at")
        if visibility not in VISIBILITIES:
            raise PublishError(f"未登记的可见范围: {visibility}")
        if snapshot_id in self.state["snapshots"]:
            raise PublishError(f"快照已发布: {snapshot_id}")
        blockers: list[str] = []
        keys = sorted(set(asset_refs))
        for key in keys:
            asset = self.state["assets"].get(key)
            if asset is None:
                blockers.append(f"资料不存在: {key}")
                continue
            if parse_ts(asset["captured_at"], "captured_at") > cutoff_dt:
                blockers.append(f"资料 {key} 采集时间晚于快照截止时间")
            if visibility == "public":
                blockers.extend(self._asset_public_blockers(asset))
            else:
                blockers.extend(self._asset_internal_blockers(asset))
        if blockers:
            raise PublishError("；".join(blockers))
        payload: dict[str, Any] = {
            "cutoff_at": cutoff_at,
            "visibility": visibility,
            "asset_refs": keys,
        }
        if purpose:
            payload["purpose"] = purpose
        named = sorted(set(named_subjects))
        if named:
            payload["named_subjects"] = named
        event, _ = self._append(
            "SNAPSHOT_PUBLISHED", "archive_snapshot", snapshot_id, occurred_at, payload, event_id=event_id
        )
        return event

    def _asset_internal_blockers(self, asset: Mapping[str, Any]) -> list[str]:
        blockers = []
        for subject in asset["subjects"]:
            consent = self.state["consents"].get(subject)
            if consent is None or "internal_research" not in consent["scope"]:
                blockers.append(f"受访者 {subject} 缺少内部研究授权依据")
        return blockers

    def _asset_public_blockers(self, asset: Mapping[str, Any]) -> list[str]:
        blockers = []
        needed = set(MEDIA_SCOPE_REQUIREMENTS[asset["media_type"]])
        if asset["status"] != "approved":
            blockers.append(f"资料 {asset['business_key']} 尚未通过专家复核")
        for subject in asset["subjects"]:
            consent = self.state["consents"].get(subject)
            if consent is None or "internal_research" not in consent["scope"]:
                blockers.append(f"受访者 {subject} 缺少内部研究授权依据")
                continue
            missing = needed - consent["scope"]
            if missing:
                blockers.append(f"受访者 {subject} 的授权范围缺少 {sorted(missing)}")
            if consent.get("is_minor"):
                guardian_scope = set(consent.get("guardian_scope", set()))
                if not consent.get("guardian") or not needed <= guardian_scope:
                    blockers.append(f"未成年受访者 {subject} 缺少监护授权")
                independent = any(
                    review["target_ref"] == asset["business_key"]
                    and review["review_kind"] == "independent"
                    and review["outcome"] == "approved"
                    for review in self.state["reviews"]
                )
                if not independent:
                    blockers.append(f"未成年受访者 {subject} 的材料缺少独立复核")
        return blockers

    # --- 服务恢复 -----------------------------------------------------

    def recover(self, now: str) -> dict[str, Any]:
        """服务恢复：重放日志后继续授权到期处理与待复核任务。"""
        parse_ts(now, "now")
        self._replay()
        expiries = self.process_consent_expiries(now)
        return {
            "recovered_at": now,
            "processed_expiries": expiries,
            "pending_reviews": self.pending_reviews(),
        }

    def pending_reviews(self) -> list[str]:
        """仍处于待复核状态的原始资料业务键。"""
        return sorted(
            key for key, asset in self.state["assets"].items() if asset["status"] == "submitted"
        )

    def process_consent_expiries(self, now: str) -> list[dict[str, Any]]:
        """处理授权到期：公开范围收回，内部研究最小事实按原依据保留。"""
        now_dt = parse_ts(now, "now")
        processed: list[dict[str, Any]] = []
        for subject in sorted(self.state["consents"]):
            consent = self.state["consents"][subject]
            valid_until = consent.get("valid_until")
            if not valid_until or parse_ts(valid_until, "valid_until") > now_dt:
                continue
            public_scopes = sorted(consent["scope"] - {"internal_research"})
            if not public_scopes:
                continue
            remaining = sorted(consent["scope"] & {"internal_research"})
            event, _ = self._append(
                "CONSENT_EXPIRED",
                "consent_record",
                subject,
                now,
                {
                    "subject_ref": subject,
                    "scope": remaining,
                    "expired_scopes": public_scopes,
                    "valid_until": valid_until,
                },
            )
            frozen = self._freeze_dependent_public_versions(
                subject, set(public_scopes), now, "consent_expired"
            )
            processed.append(
                {
                    "subject_ref": subject,
                    "expiry_event": event["event_id"],
                    "expired_scopes": public_scopes,
                    "frozen_snapshots": [e["aggregate_id"] for e in frozen],
                }
            )
        return processed
