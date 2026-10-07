import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from field_archive.contracts import load_default_schema, validate_event
from field_archive.service_cli import main

JOURNAL = ROOT / "data" / "sample_journal.jsonl"


def run_cli(*argv):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main(list(argv))
    return code, buffer.getvalue()


class ServiceCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "journal.jsonl"
        shutil.copy(JOURNAL, self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_journal_lines_satisfy_contract(self):
        schema = load_default_schema()
        for line in JOURNAL.read_text(encoding="utf-8").splitlines():
            self.assertEqual([], validate_event(json.loads(line), schema))

    def test_audit_place_names_by_historical_date(self):
        code, out = run_cli(str(self.store), "audit-place-names",
                            "--place", "place-donghu-north", "--at", "1985-06-01T00:00:00+08:00")
        self.assertEqual(0, code)
        result = json.loads(out)
        self.assertEqual("place-donghu", result["surviving_place"])
        self.assertEqual({"老鳖坑", "东湖圩"},
                         {item["name"] for item in result["names_active_at"]})

    def test_audit_conflicts_preserves_generational_views(self):
        code, out = run_cli(str(self.store), "audit-conflicts",
                            "--place", "place-donghu", "--at", "2026-10-01T00:00:00+08:00")
        self.assertEqual(0, code)
        result = json.loads(out)
        self.assertTrue(result["has_conflict"])
        self.assertEqual({"1950s", "1980s"}, set(result["narratives_by_cohort"]))
        self.assertEqual({"survey-2015", "survey-2026"},
                         set(result["judgments_by_boundary_version"]))

    def test_audit_evidence_lists_assets_and_judgments(self):
        code, out = run_cli(str(self.store), "audit-evidence",
                            "--place", "place-donghu", "--at", "2026-10-01T00:00:00+08:00")
        self.assertEqual(0, code)
        result = json.loads(out)
        keys = {asset["business_key"] for asset in result["assets"]}
        self.assertIn("donghu-interview-001", keys)
        self.assertIn("donghu-interview-002", keys)
        self.assertEqual({"survey-2015", "survey-2026"},
                         {j["boundary_version"] for j in result["spatial_judgments"]})

    def test_public_archive_is_redacted(self):
        code, out = run_cli(str(self.store), "public-archive",
                            "--at", "2026-04-15T00:00:00+08:00")
        self.assertEqual(0, code)
        archive = json.loads(out)
        self.assertEqual("snap-2026-04", archive["snapshot"])
        attributions = [name for entry in archive["entries"] for name in entry["attribution"]]
        self.assertIn("匿名受访者", attributions)
        self.assertIn("elder-wang", attributions)
        self.assertNotIn("elder-li", attributions)

    def test_recover_continues_expiry_and_pending_reviews(self):
        code, out = run_cli(str(self.store), "recover", "--now", "2026-10-07T09:00:00+08:00")
        self.assertEqual(0, code)
        report = json.loads(out)
        self.assertEqual(["donghu-photo-003"], report["pending_reviews"])
        self.assertEqual(["elder-li"], [e["subject_ref"] for e in report["processed_expiries"]])

        code, out = run_cli(str(self.store), "audit-public-scope",
                            "--at", "2026-10-07T23:59:59+08:00")
        self.assertEqual(0, code)
        scope = json.loads(out)
        self.assertEqual([], scope["available"])
        reasons = {item["snapshot"]: item["reason"] for item in scope["frozen"]}
        self.assertEqual("consent_narrowed", reasons["snap-2026-04"])
        self.assertEqual("consent_expired", reasons["snap-2026-06"])

    def test_public_scope_before_freeze_shows_available(self):
        code, out = run_cli(str(self.store), "audit-public-scope",
                            "--at", "2026-04-15T00:00:00+08:00")
        self.assertEqual(0, code)
        scope = json.loads(out)
        self.assertEqual(["snap-2026-04"], [s["snapshot"] for s in scope["available"]])
        self.assertEqual([], scope["frozen"])


if __name__ == "__main__":
    unittest.main()
