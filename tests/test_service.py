import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from field_archive.service import (
    AssignmentError,
    ClaimConflictError,
    GovernanceService,
    PublishError,
    QualificationError,
    ReviewError,
    ServiceError,
    SubmissionResponsibilityError,
    TranscriptionError,
    UploadConflictError,
)
from field_archive.store import EventIdConflictError, EventStore

T0 = "2026-01-02T09:00:00+08:00"
T_CLAIM = "2026-03-02T10:00:00+08:00"
T_UPLOAD = "2026-03-06T18:00:00+08:00"


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store_path = Path(self.tmp.name) / "journal.jsonl"
        self.service = GovernanceService(EventStore(self.store_path))

    def tearDown(self):
        self.tmp.cleanup()

    def qualify(self, student="stu-1", skill="field_collection",
                valid_from="2026-01-01T00:00:00+08:00",
                valid_until="2026-12-31T23:59:59+08:00"):
        return self.service.record_qualification(student, skill, valid_from, valid_until, T0)

    def assignment(self, assignment_id="asg-1", skill="field_collection"):
        self.service.register_assignment(assignment_id, skill, "2026-03-01T09:00:00+08:00")
        return assignment_id

    def claim(self, student="stu-1", assignment_id="asg-1"):
        return self.service.claim_assignment(assignment_id, student, T_CLAIM)

    def upload(self, key="asset-1", content_hash="hash-1",
               scope=("internal_research", "voice_public"), uploader="stu-1",
               assignment_id="asg-1", media_type="audio", subjects=("elder-1",),
               captured="2026-03-06T10:00:00+08:00", occurred=T_UPLOAD,
               place="place-1"):
        return self.service.upload_asset(
            business_key=key, content_hash=content_hash, captured_at=captured,
            uploader_ref=uploader, assignment_ref=assignment_id,
            authorized_scope=scope, media_type=media_type, subject_refs=subjects,
            place_ref=place, occurred_at=occurred,
        )

    def prepare_upload(self):
        self.qualify()
        self.assignment()
        self.claim()

    def event_types(self):
        return [event["event_type"] for event in self.service.store.events()]


class ClaimTests(ServiceTestBase):
    def test_concurrent_claim_has_single_winner(self):
        self.qualify("stu-1")
        self.qualify("stu-2")
        self.assignment()
        self.claim("stu-1")
        with self.assertRaises(ClaimConflictError):
            self.claim("stu-2")
        self.assertEqual(1, self.event_types().count("ASSIGNMENT_CLAIMED"))
        self.assertEqual("stu-1", self.service.state["assignments"]["asg-1"]["claimed_by"])

    def test_same_student_reclaim_is_idempotent(self):
        self.qualify()
        self.assignment()
        first = self.claim()
        again = self.claim()
        self.assertEqual(first["event_id"], again["event_id"])
        self.assertEqual(1, self.event_types().count("ASSIGNMENT_CLAIMED"))

    def test_release_frees_submission_responsibility(self):
        self.qualify("stu-1")
        self.qualify("stu-2")
        self.assignment()
        self.claim("stu-1")
        self.service.release_assignment("asg-1", "stu-1", "2026-03-03T09:00:00+08:00")
        self.claim("stu-2")
        self.assertEqual("stu-2", self.service.state["assignments"]["asg-1"]["claimed_by"])

    def test_release_requires_current_claimant(self):
        self.qualify("stu-1")
        self.assignment()
        self.claim("stu-1")
        with self.assertRaises(AssignmentError):
            self.service.release_assignment("asg-1", "stu-2", "2026-03-03T09:00:00+08:00")

    def test_claim_requires_qualification(self):
        self.assignment()
        with self.assertRaises(QualificationError):
            self.claim("stu-1")

    def test_claim_rejects_expired_qualification(self):
        self.qualify(valid_until="2026-02-01T00:00:00+08:00")
        self.assignment()
        with self.assertRaises(QualificationError):
            self.claim()

    def test_upload_requires_submission_responsibility(self):
        self.qualify("stu-1")
        self.qualify("stu-2")
        self.assignment()
        self.claim("stu-1")
        with self.assertRaises(SubmissionResponsibilityError):
            self.upload(uploader="stu-2")


class UploadTests(ServiceTestBase):
    def test_retry_same_event_id_is_idempotent(self):
        self.prepare_upload()
        event = self.upload()
        stored, appended = self.service.store.append(event)
        self.assertFalse(appended)
        self.assertEqual(1, self.event_types().count("ASSET_UPLOADED"))

    def test_retry_same_business_key_same_fingerprint_is_idempotent(self):
        self.prepare_upload()
        first = self.upload()
        again = self.upload()  # 不同 event_id，相同业务键+指纹+授权范围
        self.assertEqual(first["event_id"], again["event_id"])
        self.assertEqual(1, self.event_types().count("ASSET_UPLOADED"))

    def test_same_key_different_hash_is_conflicted_not_overwritten(self):
        self.prepare_upload()
        self.upload(content_hash="hash-1")
        with self.assertRaises(UploadConflictError):
            self.upload(content_hash="hash-2")
        self.assertIn("ASSET_UPLOAD_CONFLICTED", self.event_types())
        asset = self.service.state["assets"]["asset-1"]
        self.assertEqual("hash-1", asset["content_hash"])
        conflict = self.service.state["conflicts"][0]
        self.assertEqual("hash-1", conflict["existing_hash"])
        self.assertEqual("hash-2", conflict["content_hash"])

    def test_same_key_different_scope_is_conflicted(self):
        self.prepare_upload()
        self.upload(scope=("internal_research", "voice_public"))
        with self.assertRaises(UploadConflictError):
            self.upload(scope=("internal_research",))
        self.assertIn("ASSET_UPLOAD_CONFLICTED", self.event_types())

    def test_out_of_order_upload_is_accepted(self):
        self.prepare_upload()
        self.upload(key="asset-late", occurred="2026-05-01T09:00:00+08:00",
                    captured="2026-05-01T08:00:00+08:00")
        early = self.upload(key="asset-early", occurred="2026-03-01T09:00:00+08:00",
                            captured="2026-03-01T08:00:00+08:00")
        self.assertEqual("ASSET_UPLOADED", early["event_type"])
        self.assertEqual(
            ["asset-late", "asset-early"],
            [e["payload"]["business_key"] for e in self.service.store.events()
             if e["event_type"] == "ASSET_UPLOADED"],
        )

    def test_event_id_reuse_with_different_content_is_rejected(self):
        self.prepare_upload()
        event = self.upload()
        tampered = dict(event, payload=dict(event["payload"], content_hash="sha256:other"))
        with self.assertRaises(EventIdConflictError):
            self.service.store.append(tampered)


class TranscriptionTests(ServiceTestBase):
    def test_versions_increment_and_never_overwrite(self):
        self.prepare_upload()
        self.upload()
        self.service.commit_transcription("asset-1", 1, "sha256:t1", "stu-1", T_UPLOAD)
        with self.assertRaises(TranscriptionError):
            self.service.commit_transcription("asset-1", 1, "sha256:t1-alt", "stu-1", T_UPLOAD)
        with self.assertRaises(TranscriptionError):
            self.service.commit_transcription("asset-1", 3, "sha256:t3", "stu-1", T_UPLOAD)
        self.service.commit_transcription("asset-1", 2, "sha256:t2", "stu-1", T_UPLOAD)
        versions = self.service.state["transcriptions"]["asset-1"]
        self.assertEqual([1, 2], [v["transcription_version"] for v in versions])

    def test_same_version_same_hash_is_idempotent(self):
        self.prepare_upload()
        self.upload()
        first = self.service.commit_transcription("asset-1", 1, "sha256:t1", "stu-1", T_UPLOAD)
        again = self.service.commit_transcription("asset-1", 1, "sha256:t1", "stu-1", T_UPLOAD)
        self.assertEqual(first["event_id"], again["event_id"])
        self.assertEqual(1, self.event_types().count("TRANSCRIPTION_COMMITTED"))


class PlaceTests(ServiceTestBase):
    def setUp(self):
        super().setUp()
        self.service.register_place("place-1", "东湖公园", "2000-01-01T00:00:00+08:00",
                                    "2026-03-01T09:00:00+08:00")
        self.service.record_place_name("place-1", "老鳖坑", "1950-01-01T00:00:00+08:00",
                                       "2026-03-01T09:01:00+08:00",
                                       valid_to="1999-12-31T23:59:59+08:00")
        self.service.register_place("place-2", "东湖圩", "1980-01-01T00:00:00+08:00",
                                    "2026-03-01T09:02:00+08:00",
                                    valid_to="2010-12-31T23:59:59+08:00")

    def test_merge_preserves_names_and_periods(self):
        self.service.merge_places("place-1", "place-2", "2026-03-03T09:00:00+08:00")
        self.assertEqual("place-1", self.service.state["places"]["place-2"]["merged_into"])
        names_1985 = self._names_at("place-2", "1985-06-01T00:00:00+08:00")
        self.assertEqual({"老鳖坑", "东湖圩"}, set(names_1985))
        names_2026 = self._names_at("place-2", "2026-06-01T00:00:00+08:00")
        self.assertEqual({"东湖公园"}, set(names_2026))

    def _names_at(self, place_id, at):
        from field_archive.service import active_place_names
        return [item["name"] for item in active_place_names(self.service.state, place_id, at)]

    def test_merged_place_rejects_new_names(self):
        self.service.merge_places("place-1", "place-2", "2026-03-03T09:00:00+08:00")
        with self.assertRaises(ServiceError):
            self.service.record_place_name("place-2", "新名字", "2026-01-01T00:00:00+08:00",
                                           "2026-03-04T09:00:00+08:00")

    def test_double_merge_is_rejected(self):
        self.service.register_place("place-3", "东湖新村", "2011-01-01T00:00:00+08:00",
                                    "2026-03-01T09:03:00+08:00")
        self.service.merge_places("place-1", "place-2", "2026-03-03T09:00:00+08:00")
        with self.assertRaises(ServiceError):
            self.service.merge_places("place-3", "place-2", "2026-03-04T09:00:00+08:00")

    def test_narratives_from_different_cohorts_coexist(self):
        self.service.record_narrative("place-1", "elder-1", "1950s", "sha256:n1",
                                      "2026-03-06T10:30:00+08:00")
        self.service.record_narrative("place-2", "elder-2", "1980s", "sha256:n2",
                                      "2026-03-07T10:30:00+08:00")
        self.service.merge_places("place-1", "place-2", "2026-03-08T09:00:00+08:00")
        self.assertEqual(1, len(self.service.state["places"]["place-1"]["narratives"]))
        self.assertEqual(1, len(self.service.state["places"]["place-2"]["narratives"]))


class ConsentTests(ServiceTestBase):
    def setUp(self):
        super().setUp()
        self.prepare_upload()
        self.service.register_place("place-1", "东湖公园", "2000-01-01T00:00:00+08:00",
                                    "2026-03-01T09:00:00+08:00")

    def _approve(self, key="asset-1"):
        self.service.complete_review(key, "approved", "expert-1", "2026-03-10T09:00:00+08:00")

    def test_narrowing_freezes_only_dependent_public_versions(self):
        self.service.update_consent(
            "elder-1", ["internal_research", "voice_public", "name_public"],
            "2026-03-05T10:00:00+08:00")
        self.upload()
        self._approve()
        self.service.publish_snapshot("snap-public", "2026-04-01T00:00:00+08:00", "public",
                                      ["asset-1"], "2026-04-02T09:00:00+08:00",
                                      named_subjects=["elder-1"])
        self.service.publish_snapshot("snap-internal", "2026-04-01T00:00:00+08:00", "internal",
                                      ["asset-1"], "2026-04-02T09:05:00+08:00")
        self.service.update_consent("elder-1", ["internal_research"], "2026-05-01T09:00:00+08:00")
        self.assertTrue(self.service.state["snapshots"]["snap-public"]["frozen"])
        self.assertFalse(self.service.state["snapshots"]["snap-internal"]["frozen"])
        freeze = self.service.state["freezes"][0]
        self.assertEqual("consent_narrowed", freeze["reason"])
        self.assertEqual({"voice_public", "name_public"}, set(freeze["revoked_scopes"]))
        # 已用于内部研究的最小事实按原依据保留
        self.assertEqual("approved", self.service.state["assets"]["asset-1"]["status"])
        self.assertIn("asset-1", self.service.state["snapshots"]["snap-internal"]["asset_refs"])

    def test_narrowing_without_dependency_freezes_nothing(self):
        self.service.update_consent(
            "elder-1", ["internal_research", "voice_public", "name_public"],
            "2026-03-05T10:00:00+08:00")
        self.upload()
        self._approve()
        self.upload(key="asset-2", content_hash="hash-2", subjects=(), media_type="photo",
                    scope=("internal_research", "image_public"))
        self.service.complete_review("asset-2", "approved", "expert-1", "2026-03-10T09:05:00+08:00")
        self.service.publish_snapshot("snap-others", "2026-04-01T00:00:00+08:00", "public",
                                      ["asset-2"], "2026-04-02T09:00:00+08:00")
        self.service.update_consent("elder-1", ["internal_research"], "2026-05-01T09:00:00+08:00")
        self.assertFalse(self.service.state["snapshots"]["snap-others"]["frozen"])
        self.assertEqual([], self.service.state["freezes"])

    def test_narrowing_name_only_keeps_voice_snapshots(self):
        self.service.update_consent(
            "elder-1", ["internal_research", "voice_public", "name_public"],
            "2026-03-05T10:00:00+08:00")
        self.upload()
        self._approve()
        self.service.publish_snapshot("snap-voice", "2026-04-01T00:00:00+08:00", "public",
                                      ["asset-1"], "2026-04-02T09:00:00+08:00")
        self.service.update_consent("elder-1", ["internal_research", "voice_public"],
                                    "2026-05-01T09:00:00+08:00")
        self.assertFalse(self.service.state["snapshots"]["snap-voice"]["frozen"])

    def test_unknown_scope_value_is_rejected(self):
        with self.assertRaises(ServiceError):
            self.service.update_consent("elder-1", ["internal_research", "everywhere"],
                                        "2026-03-05T10:00:00+08:00")


class MinorProtectionTests(ServiceTestBase):
    def setUp(self):
        super().setUp()
        self.prepare_upload()
        self.service.register_place("place-1", "东湖公园", "2000-01-01T00:00:00+08:00",
                                    "2026-03-01T09:00:00+08:00")
        self.service.update_consent("minor-1", ["internal_research", "voice_public"],
                                    "2026-03-05T10:00:00+08:00", is_minor=True)
        self.upload(subjects=("minor-1",))
        self.service.complete_review("asset-1", "approved", "expert-1", "2026-03-10T09:00:00+08:00")

    def publish(self):
        return self.service.publish_snapshot("snap-minor", "2026-04-01T00:00:00+08:00",
                                             "public", ["asset-1"], "2026-04-02T09:00:00+08:00")

    def test_minor_material_requires_guardian_and_independent_review(self):
        with self.assertRaises(PublishError):
            self.publish()
        self.service.record_guardian_authorization(
            "minor-1", "guardian-1", ["internal_research", "voice_public"],
            "2026-03-11T09:00:00+08:00")
        with self.assertRaises(PublishError):
            self.publish()
        self.service.complete_review("asset-1", "approved", "reviewer-2",
                                     "2026-03-12T09:00:00+08:00", review_kind="independent")
        event = self.publish()
        self.assertEqual("SNAPSHOT_PUBLISHED", event["event_type"])


class ReviewTests(ServiceTestBase):
    def test_return_does_not_rewrite_original_asset(self):
        self.prepare_upload()
        self.upload(content_hash="hash-1")
        self.service.complete_review("asset-1", "returned", "expert-1",
                                     "2026-03-10T09:00:00+08:00", notes="边界需补拍")
        asset = self.service.state["assets"]["asset-1"]
        self.assertEqual("returned", asset["status"])
        self.assertEqual("hash-1", asset["content_hash"])
        original = [e for e in self.service.store.events()
                    if e["event_type"] == "ASSET_UPLOADED" and e["aggregate_id"] == "asset-1"]
        self.assertEqual(1, len(original))
        self.assertEqual("hash-1", original[0]["payload"]["content_hash"])
        # 退回后不能以同业务键改写，须以新业务键重新上传
        with self.assertRaises(UploadConflictError):
            self.upload(content_hash="hash-2")
        self.upload(key="asset-1-v2", content_hash="hash-2")
        with self.assertRaises(ReviewError):
            self.service.complete_review("asset-1", "approved", "expert-1",
                                         "2026-03-11T09:00:00+08:00")

    def test_review_requires_submitted_status(self):
        self.prepare_upload()
        with self.assertRaises(ReviewError):
            self.service.complete_review("asset-1", "approved", "expert-1",
                                         "2026-03-10T09:00:00+08:00")


class PublishTests(ServiceTestBase):
    def setUp(self):
        super().setUp()
        self.prepare_upload()
        self.service.register_place("place-1", "东湖公园", "2000-01-01T00:00:00+08:00",
                                    "2026-03-01T09:00:00+08:00")

    def test_public_snapshot_requires_expert_approval(self):
        self.service.update_consent("elder-1", ["internal_research", "voice_public"],
                                    "2026-03-05T10:00:00+08:00")
        self.upload()
        with self.assertRaises(PublishError):
            self.service.publish_snapshot("snap-1", "2026-04-01T00:00:00+08:00", "public",
                                          ["asset-1"], "2026-04-02T09:00:00+08:00")

    def test_public_snapshot_requires_voice_scope_for_audio(self):
        self.service.update_consent("elder-1", ["internal_research"], "2026-03-05T10:00:00+08:00")
        self.upload(scope=("internal_research",))
        self.service.complete_review("asset-1", "approved", "expert-1", "2026-03-10T09:00:00+08:00")
        with self.assertRaises(PublishError):
            self.service.publish_snapshot("snap-1", "2026-04-01T00:00:00+08:00", "public",
                                          ["asset-1"], "2026-04-02T09:00:00+08:00")

    def test_snapshot_rejects_assets_captured_after_cutoff(self):
        self.service.update_consent("elder-1", ["internal_research", "voice_public"],
                                    "2026-03-05T10:00:00+08:00")
        self.upload(captured="2026-05-01T10:00:00+08:00")
        self.service.complete_review("asset-1", "approved", "expert-1", "2026-05-10T09:00:00+08:00")
        with self.assertRaises(PublishError):
            self.service.publish_snapshot("snap-1", "2026-04-01T00:00:00+08:00", "public",
                                          ["asset-1"], "2026-05-11T09:00:00+08:00")


class RecoveryTests(ServiceTestBase):
    def test_restart_replays_state_and_recovery_continues_due_work(self):
        self.prepare_upload()
        self.service.register_place("place-1", "东湖公园", "2000-01-01T00:00:00+08:00",
                                    "2026-03-01T09:00:00+08:00")
        self.service.update_consent("elder-1", ["internal_research", "voice_public"],
                                    "2026-03-05T10:00:00+08:00",
                                    valid_until="2026-09-30T23:59:59+08:00")
        self.upload()
        self.service.complete_review("asset-1", "approved", "expert-1", "2026-03-10T09:00:00+08:00")
        self.service.publish_snapshot("snap-1", "2026-04-01T00:00:00+08:00", "public",
                                      ["asset-1"], "2026-04-02T09:00:00+08:00")
        self.upload(key="asset-pending", content_hash="hash-9", subjects=(),
                    scope=("internal_research",), media_type="photo",
                    captured="2026-09-20T10:00:00+08:00", occurred="2026-09-20T18:00:00+08:00")

        restarted = GovernanceService(EventStore(self.store_path))
        self.assertEqual("approved", restarted.state["assets"]["asset-1"]["status"])
        self.assertEqual("stu-1", restarted.state["assignments"]["asg-1"]["claimed_by"])

        report = restarted.recover("2026-10-07T09:00:00+08:00")
        self.assertEqual(["asset-pending"], report["pending_reviews"])
        self.assertEqual(["elder-1"], [e["subject_ref"] for e in report["processed_expiries"]])
        self.assertIn("CONSENT_EXPIRED", [e["event_type"] for e in restarted.store.events()])
        self.assertTrue(restarted.state["snapshots"]["snap-1"]["frozen"])
        self.assertEqual("consent_expired", restarted.state["snapshots"]["snap-1"]["freeze_reason"])
        # 到期后内部研究最小事实仍按原依据保留
        self.assertEqual({"internal_research"}, restarted.state["consents"]["elder-1"]["scope"])

        again = restarted.recover("2026-10-08T09:00:00+08:00")
        self.assertEqual([], again["processed_expiries"])
        self.assertEqual(
            1,
            len([e for e in restarted.store.events() if e["event_type"] == "CONSENT_EXPIRED"]),
        )


if __name__ == "__main__":
    unittest.main()
