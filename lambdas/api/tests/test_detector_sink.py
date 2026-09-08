"""The detector-side S3 sink.

Exercised here rather than only in production because the sink writes findings,
and a production run only produces findings when its window holds undeduplicated
events — an invocation can legitimately write zero and prove nothing.

The module under test is deployed inside the detector's package, so it is loaded
by path rather than imported as part of `serving`.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SINK = os.path.normpath(
    os.path.join(_HERE, "..", "..", "detector_patch", "s3_anomaly_sink.py")
)
_spec = importlib.util.spec_from_file_location("s3_anomaly_sink", _SINK)
sink = importlib.util.module_from_spec(_spec)
sys.modules["s3_anomaly_sink"] = sink
_spec.loader.exec_module(sink)


class FakeS3:
    """Captures put_object calls the way the real client would receive them."""

    def __init__(self):
        self.puts: list[dict] = []

    def put_object(self, **kw):
        self.puts.append(kw)
        return {}


def ms_at(day: str, hour: int = 12) -> int:
    """Epoch millis for a UTC day, so a test never hardcodes a guessed number."""
    d = dt.datetime.strptime(day, "%Y-%m-%d").replace(hour=hour, tzinfo=dt.timezone.utc)
    return int(d.timestamp() * 1000)


AUG30 = ms_at("2026-08-30")
AUG31 = ms_at("2026-08-31")


def finding(incident: str, ts_ms: int, severity: str = "warning",
            email: str = "a@x.com") -> dict:
    """A per-account finding, shaped as `detect._emit_anomaly` returns it."""
    return {
        "detection_kind": "azure_ad_credential_abuse_deviation",
        "severity": severity,
        "_severity": {"warning": 4, "error": 5, "critical": 6}[severity],
        "incident_id": incident,
        "user_email": email,
        "event_time_epoch": ts_ms // 1000,
        "_timestamp_ms": ts_ms,
        "reasons": [{"kind": "new_country", "why": "never seen"}],
    }


class TestWriteRun(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()

    def write(self, anomalies, audit=None):
        return sink.write_run(
            bucket="b", prefix="tenants/jiostar", run_id="run-1",
            run_at_iso="2026-08-31T12:00:00Z", anomalies=anomalies,
            audit=audit, s3_client=self.s3,
        )

    def test_findings_are_written_verbatim(self):
        """Normalization is the builder's job; the archive must not pre-chew it."""
        rec = finding("i1", AUG31, "critical")
        self.write([rec])
        self.assertEqual(len(self.s3.puts), 1)
        stored = json.loads(self.s3.puts[0]["Body"])
        self.assertEqual(stored, [rec])
        # `_severity` is the field send_batch pops; it must survive here.
        self.assertEqual(stored[0]["_severity"], 6)

    def test_key_layout_matches_the_reader(self):
        self.write([finding("i1", AUG31)])
        key = self.s3.puts[0]["Key"]
        self.assertTrue(key.startswith("tenants/jiostar/ui/anomalies/dt="), key)
        self.assertIn("/run-run-1.json", key)
        self.assertEqual(self.s3.puts[0]["ContentType"], "application/json")

    def test_findings_are_partitioned_by_the_day_they_describe(self):
        """A 00:30 run reports mostly on yesterday; buckets follow the events."""
        keys = self.write([finding("i1", AUG30), finding("i2", AUG30), finding("i3", AUG31)])
        self.assertEqual(len(keys), 2)
        by_key = {p["Key"]: json.loads(p["Body"]) for p in self.s3.puts}
        sizes = sorted(len(v) for v in by_key.values())
        self.assertEqual(sizes, [1, 2])
        self.assertTrue(any("dt=2026-08-30" in k for k in by_key))
        self.assertTrue(any("dt=2026-08-31" in k for k in by_key))

    def test_spray_finding_without_an_epoch_still_lands(self):
        """Spray findings carry only `_timestamp_ms`."""
        rec = {
            "detection_kind": "azure_ad_spray_infrastructure",
            "severity": "error", "incident_id": "s1", "ip": "9.9.9.9",
            "_timestamp_ms": AUG31,
            "reasons": [{"kind": "spray_infrastructure", "why": "one IP, many accounts"}],
        }
        self.write([rec])
        self.assertIn("dt=2026-08-31", self.s3.puts[0]["Key"])

    def test_audit_record_is_kept(self):
        """`/health` reports pipeline state from these."""
        audit = {"detection_kind": "azure_ad_credential_abuse_run_summary",
                 "run_id": "r", "anomalies_emitted": 3, "events_scored": 99}
        self.write([], audit=audit)
        bodies = [json.loads(p["Body"]) for p in self.s3.puts]
        self.assertTrue(any(audit in b for b in bodies))

    def test_zero_findings_still_records_the_run(self):
        """A quiet run is evidence the pipeline ran, not an absence of data."""
        audit = {"detection_kind": "azure_ad_credential_abuse_run_summary", "run_id": "r"}
        self.assertEqual(len(self.write([], audit=audit)), 1)

    def test_nothing_at_all_writes_nothing(self):
        self.assertEqual(self.write([]), [])
        self.assertEqual(self.s3.puts, [])

    def test_run_id_is_sanitised_into_the_key(self):
        sink.write_run(bucket="b", prefix="p", run_id="../../etc/passwd",
                       run_at_iso="x", anomalies=[finding("i", AUG31)],
                       s3_client=self.s3)
        self.assertNotIn("..", self.s3.puts[0]["Key"])

    def test_non_serialisable_values_do_not_raise(self):
        import datetime as dt

        rec = finding("i1", AUG31)
        rec["odd"] = dt.datetime(2026, 8, 31)
        self.write([rec])
        self.assertEqual(len(self.s3.puts), 1)


class TestPartitionKey(unittest.TestCase):
    def test_day_of_prefers_timestamp_ms(self):
        self.assertEqual(sink._day_of({"_timestamp_ms": AUG31}), "2026-08-31")

    def test_day_of_falls_back_to_epoch_seconds(self):
        self.assertEqual(sink._day_of({"event_time_epoch": AUG31 // 1000}), "2026-08-31")

    def test_day_of_prefers_ms_when_both_disagree(self):
        self.assertEqual(
            sink._day_of({"_timestamp_ms": AUG31, "event_time_epoch": AUG30 // 1000}),
            "2026-08-31")

    def test_day_boundary_is_utc(self):
        """23:59 UTC and 00:01 UTC the next day are different partitions."""
        self.assertEqual(sink._day_of({"_timestamp_ms": ms_at("2026-08-30", 23)}), "2026-08-30")
        self.assertEqual(sink._day_of({"_timestamp_ms": ms_at("2026-08-31", 0)}), "2026-08-31")

    def test_day_of_handles_a_missing_timestamp(self):
        self.assertRegex(sink._day_of({}), r"^\d{4}-\d{2}-\d{2}$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
