"""`/health`'s pipeline block reads both detectors' telemetry vocabularies.

`_pipeline_health` matched two hardcoded `azure_ad_*` strings. Pointed at an
Okta tenant it would have reported `last_run_at: null`, `runs_seen: 0`,
`baseline_status: null` — which the console renders as a dead pipeline, on a
detector that is running fine.

Membership against a kind set, not a per-tenant config value: the record names
its own kind, so a tenant emitting both vocabularies at once (mid-migration) is
still read correctly.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from serving import build, catalog  # noqa: E402


def run(kind, at, **extra):
    return {"detection_kind": kind, "run_at_iso": at,
            "events_scored": 10, "anomalies_emitted": 2,
            "detector_id": "det-1", **extra}


def health(kind, status, ts="2026-09-05T10:00:00Z"):
    return {"detection_kind": kind, "status": status, "ts": ts}


class KindSets(unittest.TestCase):
    def test_telemetry_is_the_union_and_keeps_its_name(self):
        """`normalize.anomaly` and `build.load_findings` both read this name."""
        self.assertEqual(catalog.TELEMETRY_KINDS,
                         catalog.RUN_SUMMARY_KINDS | catalog.BASELINE_HEALTH_KINDS)

    def test_both_vendors_are_present_in_each_half(self):
        self.assertIn("azure_ad_credential_abuse_run_summary", catalog.RUN_SUMMARY_KINDS)
        self.assertIn("okta_credential_abuse_run_summary", catalog.RUN_SUMMARY_KINDS)
        self.assertIn("azure_ad_baseline_health", catalog.BASELINE_HEALTH_KINDS)
        self.assertIn("okta_baseline_health", catalog.BASELINE_HEALTH_KINDS)

    def test_the_two_halves_do_not_overlap(self):
        self.assertEqual(catalog.RUN_SUMMARY_KINDS & catalog.BASELINE_HEALTH_KINDS,
                         frozenset())


class ReadsEachVendor(unittest.TestCase):
    def test_azure_telemetry_still_reads(self):
        out = build._pipeline_health([
            run("azure_ad_credential_abuse_run_summary", "2026-09-05T10:00:00Z"),
            health("azure_ad_baseline_health", "ok"),
        ])
        self.assertEqual(out["last_run_at"], "2026-09-05T10:00:00Z")
        self.assertEqual(out["runs_seen"], 1)
        self.assertEqual(out["baseline_status"], "ok")

    def test_okta_telemetry_reads_identically(self):
        """The regression this file exists for."""
        out = build._pipeline_health([
            run("okta_credential_abuse_run_summary", "2026-09-05T10:00:00Z"),
            health("okta_baseline_health", "degraded"),
        ])
        self.assertEqual(out["last_run_at"], "2026-09-05T10:00:00Z")
        self.assertEqual(out["runs_seen"], 1)
        self.assertEqual(out["baseline_status"], "degraded")
        self.assertEqual(out["detector_id"], "det-1")

    def test_the_newest_run_wins(self):
        out = build._pipeline_health([
            run("okta_credential_abuse_run_summary", "2026-09-05T08:00:00Z"),
            run("okta_credential_abuse_run_summary", "2026-09-05T12:00:00Z"),
            run("okta_credential_abuse_run_summary", "2026-09-05T10:00:00Z"),
        ])
        self.assertEqual(out["last_run_at"], "2026-09-05T12:00:00Z")
        self.assertEqual(out["runs_seen"], 3)

    def test_a_finding_is_not_counted_as_a_run(self):
        out = build._pipeline_health([
            {"detection_kind": "okta_credential_abuse_deviation",
             "run_at_iso": "2026-09-05T10:00:00Z"},
        ])
        self.assertEqual(out["runs_seen"], 0)
        self.assertIsNone(out["last_run_at"])

    def test_baseline_health_is_not_counted_as_a_run(self):
        out = build._pipeline_health([health("okta_baseline_health", "ok")])
        self.assertEqual(out["runs_seen"], 0)
        self.assertEqual(out["baseline_status"], "ok")

    def test_empty_telemetry_reports_nulls_rather_than_inventing(self):
        out = build._pipeline_health([])
        self.assertIsNone(out["last_run_at"])
        self.assertIsNone(out["baseline_status"])
        self.assertEqual(out["runs_seen"], 0)


class MixedVocabularies(unittest.TestCase):
    def test_both_vendors_in_one_stream_are_counted_together(self):
        """A tenant migrating from one IdP to the other keeps one honest count."""
        out = build._pipeline_health([
            run("azure_ad_credential_abuse_run_summary", "2026-09-05T08:00:00Z"),
            run("okta_credential_abuse_run_summary", "2026-09-05T12:00:00Z"),
        ])
        self.assertEqual(out["runs_seen"], 2)
        self.assertEqual(out["last_run_at"], "2026-09-05T12:00:00Z")


if __name__ == "__main__":
    unittest.main()
