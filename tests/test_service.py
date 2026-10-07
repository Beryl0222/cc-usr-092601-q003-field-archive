import io
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from field_archive import FieldArchiveService
from field_archive.cli import main as cli_main
from field_archive.errors import CertificationError, ConflictError, ConsentError, ReviewError
from field_archive.service import INTERNAL_SCOPE, load_schema
from field_archive.store import Event, EventStore


def build_world(path: str | Path, *, now: str = "2026-04-10T12:00:00+08:00") -> FieldArchiveService:
    """构造贯穿主要规则的事件世界，时间线全部显式给出。"""
    svc = FieldArchiveService(path)

    # 培训资质：s1、s2 有效；c2 同年 8 月到期，用于到期测试
    svc.grant_certification("c1", "s1", "T-ORAL", "2026-01-01T00:00:00+08:00", "2027-12-31T23:59:59+08:00")
    svc.grant_certification("c2", "s2", "T-ORAL", "2026-01-01T00:00:00+08:00", "2026-08-01T00:00:00+08:00")
    svc.grant_certification("c-photo", "sp", "T-PHOTO", "2026-01-01T00:00:00+08:00", "2027-12-31T23:59:59+08:00")

    # 任务注册与认领
    svc.register_assignment("A1", "T-ORAL", at="2026-03-01T09:00:00+08:00")
    svc.claim_assignment("A1", "s1", "c1", at="2026-04-01T09:00:00+08:00")

    # 地点称谓沿革：老鳖坑（1950-2005）与东湖公园（2005 起）并存
    svc.name_place("P", "老鳖坑", "1950-01-01T00:00:00+08:00", "2005-01-01T00:00:00+08:00", at="2026-04-02T09:00:00+08:00")
    svc.name_place("P", "东湖公园", "2005-01-01T00:00:00+08:00", None, at="2026-04-02T09:05:00+08:00")

    # 测绘边界修订
    svc.revise_boundary("B", 1, "2000-01-01T00:00:00+08:00", at="2026-04-02T10:00:00+08:00")
    svc.revise_boundary("B", 2, "2020-01-01T00:00:00+08:00", note="湖区东扩", at="2026-04-02T10:05:00+08:00")

    # 受访者 E：同时授权声音、姓名、肖像，2027 年到期；E2 同年 9 月到期
    svc.grant_consent(
        "E", ["voice_public", "identity_public", "image_public"],
        granted_at="2026-04-02T11:00:00+08:00", valid_until="2027-04-02T00:00:00+08:00",
        at="2026-04-02T11:00:00+08:00",
    )
    svc.grant_consent(
        "E2", ["voice_public"],
        granted_at="2026-04-02T11:00:00+08:00", valid_until="2026-09-01T00:00:00+08:00",
        at="2026-04-02T11:05:00+08:00",
    )

    # 素材：录音引用旧边界修订号（补拍照片引用已修订边界的情形同理保留原值）
    svc.upload_asset(
        "audio-1", "bk-audio-1", "hash-h1", "2026-04-03T08:00:00+08:00", "voice_public",
        kind="audio", place_ref="P", boundary_ref="B", boundary_revision=1,
        subject_ref="E", summary="老人讲述老鳖坑来历", uploader_ref="s1",
        at="2026-04-03T09:00:00+08:00",
    )
    svc.upload_asset(
        "photo-1", "bk-photo-1", "hash-h2", "2026-04-03T08:30:00+08:00", "image_public",
        kind="photo", place_ref="P", boundary_ref="B", boundary_revision=1,
        subject_ref="E", summary="湖区现状照片", uploader_ref="s1",
        at="2026-04-03T09:30:00+08:00",
    )
    svc.upload_asset(
        "audio-2", "bk-audio-2", "hash-h3", "2026-04-04T08:00:00+08:00", "voice_public",
        kind="audio", place_ref="P", subject_ref="E2", uploader_ref="s1",
        at="2026-04-04T09:00:00+08:00",
    )

    # 不同代际叙述并列
    svc.record_narrative("narr-elder", "P", "长辈", "2026-04-03T08:00:00+08:00",
                         asset_ref="audio-1", summary="坑是古代护城遗迹", at="2026-04-03T09:05:00+08:00")
    svc.record_narrative("narr-youth", "P", "晚辈", "2026-04-05T08:00:00+08:00",
                         summary="只记得是个养鱼的塘", at="2026-04-05T09:00:00+08:00")

    # 转写版本
    svc.add_transcript_version("t1", "audio-1", "初版转写", editor_ref="s1",
                               narrative_id="narr-elder", at="2026-04-06T09:00:00+08:00")
    svc.add_transcript_version("t1", "audio-1", "校订转写", editor_ref="s1",
                               narrative_id="narr-elder", at="2026-04-07T09:00:00+08:00")

    # 专家空间复核：素材引用 rev.1，现行已是 rev.2；退回但不改原始资料
    svc.open_review("rv-1", "asset:audio-1", kind="expert",
                    cited_boundary_ref="B", cited_revision_no=1, note="边界依据存疑",
                    at="2026-04-08T09:00:00+08:00")
    svc.return_review("rv-1", "引用的测绘边界已修订，请补充空间说明", at="2026-04-09T09:00:00+08:00")

    # 一个始终未结案的复核，用于重启后续办
    svc.open_review("rv-open", "asset:photo-1", kind="expert", at="2026-04-09T10:00:00+08:00")

    # 5 月快照
    svc.publish_snapshot("snap-1", "2026-05-01T00:00:00+08:00", at="2026-05-02T00:00:00+08:00")
    return svc


class AssignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = FieldArchiveService(self.tmp.name + "/events.jsonl")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_concurrent_claim_only_one_responsible(self) -> None:
        self.svc.grant_certification("c1", "s1", "T", "2026-01-01T00:00:00+08:00")
        for i in range(8):
            self.svc.grant_certification(f"c{i+2}", f"s{i+2}", "T", "2026-01-01T00:00:00+08:00")
        self.svc.register_assignment("A1", "T", at="2026-03-01T00:00:00+08:00")

        results: list[object] = []

        def claim(student: str, cert: str) -> None:
            try:
                results.append(self.svc.claim_assignment("A1", student, cert, at="2026-04-01T09:00:00+08:00"))
            except ConflictError as exc:
                results.append(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda i: claim(f"s{i+1}", f"c{i+1}"), range(9)))

        claim_events = [e for e in self.svc.store.stream("assign:A1") if e.event_type == "ASSIGNMENT_CLAIMED"]
        self.assertEqual(1, len(claim_events))
        winners = [r for r in results if not isinstance(r, ConflictError)]
        self.assertEqual(1, len(winners))

    def test_claim_requires_registration_and_valid_cert(self) -> None:
        self.svc.grant_certification("c1", "s1", "T", "2026-01-01T00:00:00+08:00")
        with self.assertRaises(ConflictError):
            self.svc.claim_assignment("A1", "s1", "c1", at="2026-04-01T09:00:00+08:00")
        self.svc.register_assignment("A1", "T", at="2026-03-01T00:00:00+08:00")

        # 培训方向不符
        self.svc.grant_certification("cx", "sx", "OTHER", "2026-01-01T00:00:00+08:00")
        with self.assertRaises(CertificationError):
            self.svc.claim_assignment("A1", "sx", "cx", at="2026-04-01T09:00:00+08:00")

        # 资质过期
        self.svc.grant_certification("cold", "so", "T", "2025-01-01T00:00:00+08:00", "2026-01-01T00:00:00+08:00")
        with self.assertRaises(CertificationError):
            self.svc.claim_assignment("A1", "so", "cold", at="2026-04-01T09:00:00+08:00")

        self.svc.claim_assignment("A1", "s1", "c1", at="2026-04-01T09:00:00+08:00")
        with self.assertRaises(ConflictError):
            self.svc.claim_assignment("A1", "sx", "cx", at="2026-04-02T09:00:00+08:00")

    def test_same_student_retry_is_idempotent(self) -> None:
        self.svc.grant_certification("c1", "s1", "T", "2026-01-01T00:00:00+08:00")
        self.svc.register_assignment("A1", "T", at="2026-03-01T00:00:00+08:00")
        first = self.svc.claim_assignment("A1", "s1", "c1", at="2026-04-01T09:00:00+08:00")
        second = self.svc.claim_assignment("A1", "s1", "c1", at="2026-04-01T09:00:00+08:00")
        self.assertEqual(first.event_id, second.event_id)


class UploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = FieldArchiveService(self.tmp.name + "/events.jsonl")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _upload(self, **overrides):
        kwargs = dict(
            asset_id="a1", business_key="bk-1", content_hash="h1",
            captured_at="2026-04-03T08:00:00+08:00", consent_scope="voice_public",
            at="2026-04-03T09:00:00+08:00",
        )
        kwargs.update(overrides)
        return self.svc.upload_asset(**kwargs)

    def test_retry_same_fingerprint_and_scope_is_idempotent(self) -> None:
        first = self._upload()
        second = self._upload(at="2026-04-03T10:00:00+08:00")
        self.assertEqual(first.event_id, second.event_id)
        uploads = [e for e in self.svc.store.load_all() if e.event_type == "ASSET_UPLOADED"]
        self.assertEqual(1, len(uploads))

    def test_different_fingerprint_conflicts(self) -> None:
        self._upload()
        with self.assertRaises(ConflictError):
            self._upload(content_hash="h2")

    def test_different_consent_scope_conflicts(self) -> None:
        self._upload()
        with self.assertRaises(ConflictError):
            self._upload(consent_scope="internal_research")


class PlaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = FieldArchiveService(self.tmp.name + "/events.jsonl")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_merge_keeps_old_names_and_validity(self) -> None:
        self.svc.name_place("old", "老鳖坑", "1950-01-01T00:00:00+08:00", "2005-01-01T00:00:00+08:00")
        self.svc.name_place("new", "东湖公园", "2005-01-01T00:00:00+08:00")
        self.svc.record_narrative("n1", "old", "长辈", "2026-04-03T08:00:00+08:00", summary="旧说法")
        self.svc.merge_places("new", ["old"], "2026-04-08T00:00:00+08:00")

        old = self.svc.state["places"]["place:old"]
        self.assertEqual("new", old["merged_into"]["surviving_place_id"])
        self.assertEqual(["老鳖坑"], [n["name"] for n in old["names"]])
        self.assertEqual("2005-01-01T00:00:00+08:00", old["names"][0]["valid_until"])
        self.assertEqual(1, len(old["narratives"]))  # 旧地点的叙述未被覆盖

        with self.assertRaises(ConflictError):
            self.svc.merge_places("new", ["old"], "2026-04-09T00:00:00+08:00")

    def test_boundary_revision_must_advance(self) -> None:
        self.svc.revise_boundary("B", 1, "2000-01-01T00:00:00+08:00")
        with self.assertRaises(ConflictError):
            self.svc.revise_boundary("B", 1, "2001-01-01T00:00:00+08:00")

    def test_generational_narratives_are_not_collapsed(self) -> None:
        self.svc.record_narrative("n1", "P", "长辈", "2026-04-03T08:00:00+08:00", summary="说法甲")
        self.svc.record_narrative("n2", "P", "晚辈", "2026-04-04T08:00:00+08:00", summary="说法乙")
        narratives = self.svc.state["places"]["place:P"]["narratives"]
        self.assertEqual(["说法甲", "说法乙"], [n["summary"] for n in narratives])


class ConsentLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + "/events.jsonl"
        self.svc = build_world(self.path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_snapshot_takes_effective_scope_at_cutoff(self) -> None:
        snap = self.svc.state["snapshots"]["snapshot:snap-1"]
        refs = {e["ref"] for e in snap["entries"]}
        self.assertIn("asset:audio-1#voice_public", refs)
        self.assertIn("asset:photo-1#image_public", refs)
        self.assertIn("asset:audio-2#voice_public", refs)

    def test_narrow_identity_keeps_voice_but_hides_name(self) -> None:
        self.svc.narrow_consent("E", ["identity_public"], "2026-06-01T00:00:00+08:00", reason="老人要求匿名")

        snap = self.svc.state["snapshots"]["snapshot:snap-1"]
        # 收窄身份：声音/肖像条目都不冻结
        self.assertEqual(set(), self.svc._frozen_entry_refs(snap))

        catalog = self.svc.public_catalog("2026-06-15T00:00:00+08:00")
        records = {r["ref"]: r for r in catalog["records"]}
        self.assertIn("asset:audio-1", records)
        self.assertFalse(records["asset:audio-1"]["identified"])
        self.assertTrue(records["asset:audio-1"]["subject"].startswith("受访者-"))
        self.assertEqual(["voice_public"], records["asset:audio-1"]["aspects"])
        self.assertIn("asset:photo-1", records)

    def test_narrow_voice_freezes_only_voice_version(self) -> None:
        self.svc.narrow_consent("E", ["identity_public"], "2026-06-01T00:00:00+08:00")
        self.svc.narrow_consent("E", ["voice_public"], "2026-07-01T00:00:00+08:00", reason="撤回声音公开")

        snap = self.svc.state["snapshots"]["snapshot:snap-1"]
        frozen = self.svc._frozen_entry_refs(snap)
        self.assertIn("asset:audio-1#voice_public", frozen)
        self.assertNotIn("asset:photo-1#image_public", frozen)

        catalog = self.svc.public_catalog("2026-07-15T00:00:00+08:00")
        refs = {r["ref"] for r in catalog["records"]}
        self.assertNotIn("asset:audio-1", refs)  # 声音版本冻结
        self.assertIn("asset:photo-1", refs)     # 肖像仍公开

        # 内部研究最小事实按原依据保留
        view = self.svc.rebuild_as_of("2026-07-15T00:00:00+08:00")
        self.assertIn(INTERNAL_SCOPE, view["consents"]["E"]["scopes_available"])
        self.assertEqual("hash-h1", self.svc.state["assets"]["asset:audio-1"]["content_hash"])

    def test_expiry_resumes_after_restart_and_freezes_dependents(self) -> None:
        reloaded = FieldArchiveService(self.path)
        report = reloaded.run_due("2026-10-01T00:00:00+08:00")
        self.assertEqual(["E2"], report["expired_consents"])
        self.assertIn("c2", report["expired_certifications"])
        self.assertEqual(1, report["frozen_entries"])
        pending = {r["review_case_id"] for r in report["pending_reviews"]}
        self.assertIn("rv-open", pending)

        snap = reloaded.state["snapshots"]["snapshot:snap-1"]
        self.assertIn("asset:audio-2#voice_public", reloaded._frozen_entry_refs(snap))

        # 到期后资质不可再用（用一个新注册、尚未认领的任务验证）
        reloaded.register_assignment("A2", "T-ORAL", at="2026-10-02T08:00:00+08:00")
        with self.assertRaises(CertificationError):
            reloaded.claim_assignment("A2", "s2", "c2", at="2026-10-02T09:00:00+08:00")

        # 待复核任务可继续处理
        reloaded.complete_review("rv-open", "approved", at="2026-10-03T00:00:00+08:00")

        # 恢复推进是幂等的
        again = FieldArchiveService(self.path).run_due("2026-10-01T00:00:00+08:00")
        self.assertEqual([], again["expired_consents"])
        self.assertEqual([], again["expired_certifications"])
        self.assertEqual(0, again["frozen_entries"])

        # 更晚时刻：E 到期，冻结其声音与肖像两个公开条目
        later = FieldArchiveService(self.path).run_due("2027-05-01T00:00:00+08:00")
        self.assertEqual(["E"], later["expired_consents"])
        self.assertEqual(2, later["frozen_entries"])


class ReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = build_world(self.tmp.name + "/events.jsonl")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_return_does_not_rewrite_raw_asset(self) -> None:
        before = self.svc.state["assets"]["asset:audio-1"]
        snapshot_before = json.dumps(before, sort_keys=True, ensure_ascii=False)
        events_before = len(self.svc.store.stream("asset:audio-1"))

        review = self.svc.state["reviews"]["review:rv-1"]
        self.assertEqual("returned", review["status"])
        self.assertEqual(1, review["rounds"])
        self.assertEqual(1, review["notes"][0]["cited_revision_no"])
        self.assertEqual(2, review["notes"][0]["current_revision_no"])
        self.assertIn("已修订", review["returns"][0]["reason"])

        # 原始素材没有任何新事件、内容不变
        self.assertEqual(events_before, len(self.svc.store.stream("asset:audio-1")))
        self.assertEqual(snapshot_before, json.dumps(self.svc.state["assets"]["asset:audio-1"], sort_keys=True, ensure_ascii=False))

    def test_completed_case_cannot_be_returned(self) -> None:
        self.svc.complete_review("rv-1", "approved", at="2026-04-10T09:00:00+08:00")
        with self.assertRaises(ReviewError):
            self.svc.return_review("rv-1", "再退回", at="2026-04-11T09:00:00+08:00")

    def test_unknown_case_and_bad_decision(self) -> None:
        with self.assertRaises(ReviewError):
            self.svc.return_review("nope", "x")
        with self.assertRaises(ReviewError):
            self.svc.complete_review("rv-1", "maybe")


class MinorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = FieldArchiveService(self.tmp.name + "/events.jsonl")
        # 监护人授权先存在
        self.svc.grant_consent("G", ["identity_public"], granted_at="2026-04-01T00:00:00+08:00",
                               at="2026-04-01T09:00:00+08:00")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_minor_requires_guardian_consent(self) -> None:
        with self.assertRaises(ConsentError):
            self.svc.grant_consent("M", ["voice_public"], is_minor=True,
                                   at="2026-04-02T09:00:00+08:00")
        with self.assertRaises(ConsentError):
            self.svc.grant_consent("M2", ["voice_public"], is_minor=True, guardian_subject_ref="NOBODY",
                                   at="2026-04-02T09:00:00+08:00")
        self.svc.grant_consent("M", ["voice_public"], is_minor=True, guardian_subject_ref="G",
                               at="2026-04-02T09:00:00+08:00")
        self.assertTrue(self.svc.state["consents"]["consent:M"]["is_minor"])

    def test_minor_material_needs_independent_review_before_public(self) -> None:
        self.svc.grant_consent("M", ["voice_public"], is_minor=True, guardian_subject_ref="G",
                               at="2026-04-02T09:00:00+08:00")
        self.svc.upload_asset("minor-audio", "bk-minor", "hm", "2026-04-03T08:00:00+08:00",
                              "voice_public", kind="audio", subject_ref="M",
                              at="2026-04-03T09:00:00+08:00")
        self.svc.publish_snapshot("snap-before", "2026-05-01T00:00:00+08:00", at="2026-05-02T00:00:00+08:00")
        refs_before = {e["ref"] for e in self.svc.state["snapshots"]["snapshot:snap-before"]["entries"]}
        self.assertNotIn("asset:minor-audio#voice_public", refs_before)

        # 内部研究最小事实仍然保留
        view = self.svc.rebuild_as_of("2026-05-01T00:00:00+08:00")
        self.assertIn(INTERNAL_SCOPE, view["consents"]["M"]["scopes_available"])

        # 独立复核通过后，新快照可以公开
        self.svc.open_review("rv-minor", "asset:minor-audio", kind="independent",
                             at="2026-05-03T09:00:00+08:00")
        self.svc.complete_review("rv-minor", "approved", at="2026-05-04T09:00:00+08:00")
        self.svc.publish_snapshot("snap-after", "2026-06-01T00:00:00+08:00", at="2026-06-02T00:00:00+08:00")
        refs_after = {e["ref"] for e in self.svc.state["snapshots"]["snapshot:snap-after"]["entries"]}
        self.assertIn("asset:minor-audio#voice_public", refs_after)


class AuditRebuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = build_world(self.tmp.name + "/events.jsonl")
        self.svc.narrow_consent("E", ["identity_public"], "2026-06-01T00:00:00+08:00")
        self.svc.narrow_consent("E", ["voice_public"], "2026-07-01T00:00:00+08:00")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_historical_names_validity_and_boundary(self) -> None:
        # 2026-04-10：两个称谓都已入档；按有效期窗口，老鳖坑已停用、东湖公园在用
        view = self.svc.rebuild_as_of("2026-04-10T00:00:00+08:00")
        place = view["places"]["P"]
        names = {n["name"]: n for n in place["names"]}
        self.assertFalse(names["老鳖坑"]["in_use"])
        self.assertEqual("2005-01-01T00:00:00+08:00", names["老鳖坑"]["valid_until"])
        self.assertTrue(names["东湖公园"]["in_use"])
        self.assertEqual(2, view["boundaries"]["B"]["current_revision_no"])

        # 旧称与依据事件不会因合并或新称而丢失
        self.assertTrue(all(n["basis_event"] for n in place["names"]))

    def test_conflicting_views_and_evidence_sources(self) -> None:
        view = self.svc.rebuild_as_of("2026-04-10T00:00:00+08:00")
        place = view["places"]["P"]
        generations = {v["generation"] for v in place["conflicting_views"]}
        self.assertEqual({"长辈", "晚辈"}, generations)

        elder = next(n for n in place["narratives"] if n["narrative_id"] == "narr-elder")
        self.assertEqual("hash-h1", elder["evidence"]["source_asset"]["content_hash"])
        self.assertEqual(1, elder["evidence"]["source_asset"]["boundary_revision"])
        versions = elder["evidence"]["transcript_versions"]
        self.assertEqual([1, 2], [v["version_no"] for v in versions])

    def test_public_range_reflects_date(self) -> None:
        june = self.svc.rebuild_as_of("2026-06-15T00:00:00+08:00")
        snap = june["public_range"]["snapshots"][0]
        self.assertIn("asset:audio-1#voice_public", snap["available_entries"])
        self.assertEqual([], snap["frozen_entries"])
        self.assertEqual(
            sorted(["voice_public", "image_public", INTERNAL_SCOPE]),
            june["consents"]["E"]["scopes_available"],
        )

        july = self.svc.rebuild_as_of("2026-07-15T00:00:00+08:00")
        snap = july["public_range"]["snapshots"][0]
        self.assertIn("asset:audio-1#voice_public", snap["frozen_entries"])
        self.assertNotIn("asset:audio-1#voice_public", snap["available_entries"])
        self.assertIn("asset:photo-1#image_public", snap["available_entries"])

    def test_audit_uses_only_past_events(self) -> None:
        early = self.svc.rebuild_as_of("2026-03-15T00:00:00+08:00")
        self.assertEqual({}, early["places"])
        self.assertEqual({}, early["consents"])
        self.assertEqual([], early["public_range"]["snapshots"])


class StoreAndCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + "/events.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_contract_violation_and_version_conflict(self) -> None:
        store = EventStore(self.path, schema=load_schema())
        good = Event("e1", "PLACE_NAMED", "place_record", "place:P", "2026-04-01T09:00:00+08:00", 1,
                     {"place_id": "P", "name": "x", "valid_from": "2026-04-01T09:00:00+08:00"})
        store.append(good)
        bad = Event("e2", "PLACE_NAMED", "place_record", "place:Q", "2026-04-01 09:00:00", 1, {})
        from field_archive.errors import ContractViolation
        with self.assertRaises(ContractViolation):
            store.append(bad)
        clash = Event("e3", "PLACE_NAMED", "place_record", "place:P", "2026-04-02T09:00:00+08:00", 1,
                      {"place_id": "P", "name": "y", "valid_from": "2026-04-02T09:00:00+08:00"})
        from field_archive.errors import VersionConflict
        with self.assertRaises(VersionConflict):
            store.append(clash)
        # event_id 重复：直接返回旧事件
        self.assertEqual("e1", store.append(good).event_id)

    def test_cli_audit_outputs_json(self) -> None:
        build_world(self.path)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main(["audit", self.path, "--as-of", "2026-06-15T00:00:00+08:00"])
        self.assertEqual(0, code)
        view = json.loads(buf.getvalue())
        self.assertIn("places", view)
        self.assertEqual("2026-06-15T00:00:00+08:00", view["as_of"])

    def test_cli_public_and_run_due(self) -> None:
        build_world(self.path)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main(["public", self.path, "--as-of", "2026-05-15T00:00:00+08:00"])
        self.assertEqual(0, code)
        catalog = json.loads(buf.getvalue())
        refs = {r["ref"] for r in catalog["records"]}
        self.assertEqual({"asset:audio-1", "asset:photo-1", "asset:audio-2"}, refs)
        audio = next(r for r in catalog["records"] if r["ref"] == "asset:audio-1")
        self.assertTrue(audio["identified"])  # 5 月时姓名授权仍有效

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main(["run-due", self.path, "--as-of", "2026-10-01T00:00:00+08:00"])
        self.assertEqual(0, code)
        report = json.loads(buf.getvalue())
        self.assertEqual(["E2"], report["expired_consents"])


if __name__ == "__main__":
    unittest.main()
