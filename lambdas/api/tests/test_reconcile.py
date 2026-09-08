"""The reconciliation filter chain.

`reconcile.classify` exists to attribute the gap between the Coralogix log
stream and the UI's anomaly count to a named stage, so its only real invariant
is that it is *exhaustive*: every record read lands in exactly one bucket. If a
stage ever swallows a record silently the ledger stops summing and the residual
gets blamed on snapshot lag, which is the one failure mode that would make the
whole script lie.

The chain deliberately mirrors `serving/build.py::load_findings` step for step,
so these cases double as a pin on that order.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import reconcile  # noqa: E402
from serving import links, tenants  # noqa: E402

# `classify` runs each record through `normalize.anomaly`, which builds
# per-tenant Coralogix links. These tests are about the stage a record lands in,
# not the links, so they pass one tenant's config once here.
_LINKS = links.for_tenant(tenants.get("jiostar"))


def classify_(records, floor_ms):
    return reconcile.classify(records, floor_ms, _LINKS)


def ms_at(day: str, hour: int = 12) -> int:
    d = dt.datetime.strptime(day, "%Y-%m-%d").replace(hour=hour, tzinfo=dt.timezone.utc)
    return int(d.timestamp() * 1000)


AUG30 = ms_at("2026-08-30")
AUG31 = ms_at("2026-08-31")
SEP01 = ms_at("2026-09-01")
# A floor between AUG31 and SEP01, so AUG31 findings are outside the window.
FLOOR = ms_at("2026-08-31", 18)


def finding(incident: str, ts_ms: int = SEP01, **over) -> dict:
    rec = {
        "detection_kind": "azure_ad_credential_abuse_deviation",
        "severity": "warning",
        "incident_id": incident,
        "user_email": "a@x.com",
        "event_time_epoch": ts_ms // 1000,
        "_timestamp_ms": ts_ms,
        "reasons": [{"kind": "new_app", "why": "never seen"}],
    }
    rec.update(over)
    return rec


def telemetry(kind: str = "azure_ad_credential_abuse_run_summary") -> dict:
    return {"detection_kind": kind, "run_id": "r", "anomalies_emitted": 3}


class TestExhaustive(unittest.TestCase):
    """Every record read must land in exactly one bucket."""

    def test_a_mixed_batch_balances(self):
        records = [
            finding("i1"), finding("i2"),
            finding("i1"),                       # duplicate
            finding("i3", AUG31),                # outside the window
            telemetry(), telemetry("azure_ad_baseline_health"),
            finding("i4", ts_ms=0, _timestamp_ms=0, event_time_epoch=0),
            {"detection_kind": "azure_ad_credential_abuse_deviation"},  # no id
            "not a record",                      # not counted at all
        ]
        st = classify_(records, FLOOR)
        self.assertTrue(st.balances(), st)
        self.assertEqual(st.read, 8)
        self.assertEqual(st.kept_n, 2)

    def test_balances_on_an_empty_batch(self):
        st = classify_([], FLOOR)
        self.assertTrue(st.balances())
        self.assertEqual((st.read, st.kept_n), (0, 0))

    def test_non_dicts_are_not_counted_as_read(self):
        """`build.load_findings` skips them before it increments its raw count."""
        st = classify_(["x", None, 3, finding("i1")], FLOOR)
        self.assertEqual(st.read, 1)
        self.assertTrue(st.balances())


class TestOneStageEach(unittest.TestCase):
    """Each drop reason is attributed to its own stage and no other."""

    def only(self, records, field: str, n: int = 1):
        st = classify_(records, FLOOR)
        self.assertEqual(getattr(st, field), n, f"{field} in {st}")
        for f, _ in reconcile.DROP_STAGES:
            if f != field:
                self.assertEqual(getattr(st, f), 0, f"{f} should be 0 in {st}")
        return st

    def test_run_summary_is_telemetry_only(self):
        st = self.only([telemetry()], "telemetry")
        self.assertEqual(st.kept_n, 0)

    def test_baseline_health_is_telemetry_only(self):
        self.only([telemetry("azure_ad_baseline_health")], "telemetry")

    def test_a_record_with_no_id_is_attributed_once(self):
        self.only([{"detection_kind": "azure_ad_credential_abuse_deviation",
                    "_timestamp_ms": SEP01}], "no_incident_id")

    def test_event_id_alone_is_enough_to_survive(self):
        """`normalize.anomaly` falls back to `event_id`, so this is not a drop."""
        rec = finding("x", SEP01)
        del rec["incident_id"]
        rec["event_id"] = "e1"
        st = classify_([rec], FLOOR)
        self.assertEqual((st.kept_n, st.no_incident_id), (1, 0))

    def test_a_record_with_no_usable_timestamp_is_attributed_once(self):
        self.only([finding("i1", ts_ms=0, _timestamp_ms=0, event_time_epoch=0)],
                  "no_timestamp")

    def test_a_repeated_incident_id_is_attributed_once(self):
        st = self.only([finding("i1"), finding("i1")], "duplicate_incident_id")
        self.assertEqual(st.kept_n, 1)

    def test_a_finding_before_the_floor_is_attributed_once(self):
        self.only([finding("i1", AUG30)], "outside_window")


class TestOrdering(unittest.TestCase):
    """The chain's order is load-bearing: a record must not be double-blamed."""

    def test_telemetry_wins_over_a_missing_id(self):
        """Telemetry carries no `incident_id`; it must not count as an id drop."""
        st = classify_([telemetry()], FLOOR)
        self.assertEqual((st.telemetry, st.no_incident_id), (1, 0))

    def test_dedup_precedes_the_window_as_in_the_builder(self):
        """`build.load_findings` dedups over its whole read; the window is later."""
        st = classify_([finding("i1", AUG30), finding("i1", AUG30)], FLOOR)
        self.assertEqual((st.duplicate_incident_id, st.outside_window), (1, 1))
        self.assertEqual(st.kept_n, 0)

    def test_a_finding_exactly_on_the_floor_is_inside_the_window(self):
        """`routes.anomalies` uses `>= floor`, so the boundary is inclusive."""
        st = classify_([finding("i1", FLOOR)], FLOOR)
        self.assertEqual((st.kept_n, st.outside_window), (1, 0))


class TestBreakdown(unittest.TestCase):
    def test_kept_records_are_tallied_by_detection_kind(self):
        st = classify_(
            [finding("i1"), finding("i2"),
             finding("i3", detection_kind="azure_ad_spray_infrastructure"),
             finding("i4", AUG30)],   # outside the window: not tallied
            FLOOR)
        self.assertEqual(st.kinds["azure_ad_credential_abuse_deviation"], 2)
        self.assertEqual(st.kinds["azure_ad_spray_infrastructure"], 1)
        self.assertEqual(sum(st.kinds.values()), st.kept_n)

    def test_as_dict_reports_every_stage(self):
        d = classify_([finding("i1"), telemetry()], FLOOR).as_dict()
        for field, _ in reconcile.DROP_STAGES:
            self.assertIn(field, d)
        self.assertEqual(d["read"], d["findings_in_window"]
                         + sum(d[f] for f, _ in reconcile.DROP_STAGES))


if __name__ == "__main__":
    unittest.main(verbosity=2)
