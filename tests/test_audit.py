import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from field_archive.audit import (
    build_state_as_of,
    conflicting_viewpoints_at,
    evidence_sources_at,
    place_names_at,
    public_scope_at,
)
from field_archive.public_api import public_archive
from field_archive.service import GovernanceService
from field_archive.store import EventStore


def build_journal(service):
    """搭建含地点合并、收窄许可与修订边界的完整日志。"""
    service.record_qualification("stu-1", "field_collection",
                                 "2026-01-01T00:00:00+08:00", "2026-12-31T23:59:59+08:00",
                                 "2026-01-02T09:00:00+08:00")
    service.register_assignment("asg-1", "field_collection", "2026-03-01T09:00:00+08:00")
    service.claim_assignment("asg-1", "stu-1", "2026-03-02T10:00:00+08:00")
    service.register_place("place-1", "东湖公园", "2000-01-01T00:00:00+08:00",
                           "2026-03-01T09:30:00+08:00")
    service.record_place_name("place-1", "老鳖坑", "1950-01-01T00:00:00+08:00",
                              "2026-03-01T09:31:00+08:00", valid_to="1999-12-31T23:59:59+08:00")
    service.register_place("place-2", "东湖圩", "1980-01-01T00:00:00+08:00",
                           "2026-03-01T09:32:00+08:00", valid_to="2010-12-31T23:59:59+08:00")
    service.merge_places("place-1", "place-2", "2026-03-03T09:00:00+08:00")
    service.update_consent("elder-li", ["internal_research", "voice_public"],
                           "2026-03-05T10:00:00+08:00")
    service.update_consent("elder-wang", ["internal_research", "voice_public", "name_public"],
                           "2026-03-05T10:05:00+08:00")
    service.upload_asset(business_key="asset-li", content_hash="sha256:a1",
                         captured_at="2026-03-06T10:00:00+08:00", uploader_ref="stu-1",
                         assignment_ref="asg-1",
                         authorized_scope=["internal_research", "voice_public"],
                         media_type="audio", subject_refs=["elder-li"], place_ref="place-1",
                         occurred_at="2026-03-06T18:00:00+08:00")
    service.upload_asset(business_key="asset-wang", content_hash="sha256:b2",
                         captured_at="2026-03-07T10:00:00+08:00", uploader_ref="stu-1",
                         assignment_ref="asg-1",
                         authorized_scope=["internal_research", "voice_public", "name_public"],
                         media_type="audio", subject_refs=["elder-wang"], place_ref="place-2",
                         occurred_at="2026-03-07T18:00:00+08:00")
    service.commit_transcription("asset-li", 1, "sha256:t1", "stu-1", "2026-03-08T09:00:00+08:00")
    service.record_narrative("place-1", "elder-li", "1950s", "sha256:narr-1",
                             "2026-03-06T10:30:00+08:00")
    service.record_narrative("place-2", "elder-wang", "1980s", "sha256:narr-2",
                             "2026-03-07T10:30:00+08:00")
    service.record_spatial_judgment("judg-1", "place-1", "survey-2015",
                                    "北界沿用2015年测绘老堤线", "geo-1",
                                    "2026-03-09T09:00:00+08:00")
    service.record_spatial_judgment("judg-2", "place-1", "survey-2026",
                                    "北界按2026年修订边界外移30米", "geo-2",
                                    "2026-03-09T10:00:00+08:00")
    service.complete_review("asset-li", "approved", "expert-1", "2026-03-10T09:00:00+08:00")
    service.complete_review("asset-wang", "approved", "expert-1", "2026-03-10T09:05:00+08:00")
    service.publish_snapshot("snap-1", "2026-04-01T00:00:00+08:00", "public",
                             ["asset-li", "asset-wang"], "2026-04-02T09:00:00+08:00",
                             named_subjects=["elder-wang"])
    service.update_consent("elder-wang", ["internal_research", "voice_public"],
                           "2026-05-01T09:00:00+08:00")


class AuditTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = GovernanceService(EventStore(Path(self.tmp.name) / "journal.jsonl"))
        build_journal(self.service)
        self.events = self.service.store.events

    def tearDown(self):
        self.tmp.cleanup()

    def state_at(self, at):
        return build_state_as_of(self.events(), at)


class PlaceNameAuditTests(AuditTestBase):
    def test_names_reconstructed_by_validity_period(self):
        state = self.state_at("2026-10-01T00:00:00+08:00")
        result = place_names_at(state, "place-2", "1985-06-01T00:00:00+08:00")
        self.assertEqual("place-1", result["surviving_place"])
        self.assertEqual({"老鳖坑", "东湖圩"},
                         {item["name"] for item in result["names_active_at"]})
        result_now = place_names_at(state, "place-2", "2026-06-01T00:00:00+08:00")
        self.assertEqual(["东湖公园"], [item["name"] for item in result_now["names_active_at"]])
        self.assertEqual(3, len(result_now["timeline"]))

    def test_as_of_before_merge_place_stands_alone(self):
        state = self.state_at("2026-03-02T23:59:59+08:00")
        result = place_names_at(state, "place-2", "2026-03-02T23:59:59+08:00")
        self.assertEqual("place-2", result["surviving_place"])
        self.assertEqual(["东湖圩"], [item["name"] for item in result["timeline"]])


class EvidenceAuditTests(AuditTestBase):
    def test_evidence_sources_as_of_date(self):
        state = self.state_at("2026-03-06T23:59:59+08:00")
        result = evidence_sources_at(state, "place-1")
        self.assertEqual(["asset-li"], [a["business_key"] for a in result["assets"]])
        self.assertEqual({}, result["transcriptions"])
        self.assertEqual([], result["spatial_judgments"])

    def test_evidence_includes_merged_place_and_boundary_versions(self):
        state = self.state_at("2026-10-01T00:00:00+08:00")
        result = evidence_sources_at(state, "place-1")
        self.assertEqual({"place-1", "place-2"}, set(result["place_members"]))
        self.assertEqual({"asset-li", "asset-wang"},
                         {a["business_key"] for a in result["assets"]})
        self.assertEqual([1], [t["transcription_version"]
                               for t in result["transcriptions"]["asset-li"]])
        versions = {j["boundary_version"] for j in result["spatial_judgments"]}
        self.assertEqual({"survey-2015", "survey-2026"}, versions)
        self.assertEqual(2, len(result["reviews"]))

    def test_late_arriving_upload_slots_into_history(self):
        self.service.upload_asset(business_key="asset-late", content_hash="sha256:c3",
                                  captured_at="2026-03-04T10:00:00+08:00", uploader_ref="stu-1",
                                  assignment_ref="asg-1", authorized_scope=["internal_research"],
                                  media_type="photo", subject_refs=[], place_ref="place-1",
                                  occurred_at="2026-03-04T18:00:00+08:00")
        state = self.state_at("2026-03-05T00:00:00+08:00")
        result = evidence_sources_at(state, "place-1")
        self.assertEqual(["asset-late"], [a["business_key"] for a in result["assets"]])


class ConflictAuditTests(AuditTestBase):
    def test_conflicting_viewpoints_are_preserved_not_merged(self):
        state = self.state_at("2026-10-01T00:00:00+08:00")
        result = conflicting_viewpoints_at(state, "place-1")
        self.assertTrue(result["has_conflict"])
        self.assertEqual({"1950s", "1980s"}, set(result["narratives_by_cohort"]))
        self.assertEqual({"survey-2015", "survey-2026"},
                         set(result["judgments_by_boundary_version"]))
        self.assertEqual(2, result["distinct_narrative_views"])
        self.assertEqual(2, result["distinct_spatial_views"])

    def test_no_conflict_before_second_view(self):
        state = self.state_at("2026-03-06T23:59:59+08:00")
        result = conflicting_viewpoints_at(state, "place-1")
        self.assertFalse(result["has_conflict"])
        self.assertEqual(1, result["distinct_narrative_views"])


class PublicScopeAuditTests(AuditTestBase):
    def test_public_scope_changes_with_freeze(self):
        before = public_scope_at(self.state_at("2026-04-15T00:00:00+08:00"),
                                 "2026-04-15T00:00:00+08:00")
        self.assertEqual(["snap-1"], [s["snapshot"] for s in before["available"]])
        self.assertEqual([], before["frozen"])
        after = public_scope_at(self.state_at("2026-10-01T00:00:00+08:00"),
                                "2026-10-01T00:00:00+08:00")
        self.assertEqual([], after["available"])
        self.assertEqual(["snap-1"], [s["snapshot"] for s in after["frozen"]])
        self.assertEqual("consent_narrowed", after["frozen"][0]["reason"])

    def test_public_archive_redacts_names_without_name_consent(self):
        state = self.state_at("2026-04-15T00:00:00+08:00")
        archive = public_archive(state, "2026-04-15T00:00:00+08:00")
        self.assertEqual("snap-1", archive["snapshot"])
        by_asset = {entry["asset"]: entry for entry in archive["entries"]}
        self.assertEqual(["匿名受访者"], by_asset["asset-li"]["attribution"])
        self.assertEqual(["elder-wang"], by_asset["asset-wang"]["attribution"])
        self.assertEqual(["东湖公园"], by_asset["asset-li"]["place_names"])

    def test_public_archive_empty_after_freeze(self):
        state = self.state_at("2026-10-01T00:00:00+08:00")
        archive = public_archive(state, "2026-10-01T00:00:00+08:00")
        self.assertIsNone(archive["snapshot"])
        self.assertEqual([], archive["entries"])


if __name__ == "__main__":
    unittest.main()
