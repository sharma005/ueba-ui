"""The builder's 30-day window, asserted against the store's day partitions.

Regression cover for a real defect: the snapshot in S3 reported 30,351 findings
where the same code over the same data and the same clock counts 30,088. The
263 extra all sat in the oldest `dt=` partition, below `now - WINDOW_DAYS` —
the store is partitioned by day, so the oldest key pulled in carries up to a
full extra day of findings on the wrong side of the cutoff.

That matters beyond the total: `n_anomalies` is what the roster card prints,
and a count anchored to a partition edge rather than to a timestamp cannot be
reproduced by any client slicing the same window, so an entity page and the
card disagreed (27 against 26) with neither of them wrong about its own rule.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from serving import build, derive, tenants  # noqa: E402

T = tenants.get("jiostar")

DAY_MS = 86_400_000
NOW = 1_788_258_665_412  # 2026-09-01T10:31:05.412Z — mid-day, so the edge bites.
FLOOR = NOW - derive.WINDOW_DAYS * DAY_MS


def _day(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


def _rec(ts_ms: int, ident: str) -> dict:
    """The smallest record `normalize.anomaly` will accept and keep.

    `_timestamp_ms` is the field the detector actually sets and the only one
    `normalize.ts_ms` prefers; a record without it normalizes to `ts == 0` and
    is dropped for that reason instead of by the window, which would make these
    assertions pass for the wrong reason.
    """
    return {
        "incident_id": ident,
        "detection_kind": "azure_ad_credential_abuse_deviation",
        "user_email": "u@x.com",
        "_timestamp_ms": ts_ms,
        "severity": "warning",
        "reasons": [{"kind": "unusual_hour", "why": "test"}],
    }


class WindowEdge(unittest.TestCase):
    """`load_findings` filters on timestamp, not on the partition it arrived in."""

    def _load(self, records: list[dict]) -> list[dict]:
        # One key per day, as the real store is laid out.
        by_day: dict[str, list[dict]] = {}
        for r in records:
            by_day.setdefault(_day(r["_timestamp_ms"]), []).append(r)
        keys = [f"anomalies/dt={d}/part.json" for d in sorted(by_day)]
        blobs = [by_day[k.split("/dt=")[1].split("/")[0]] for k in keys]

        with mock.patch.object(build.s3store, "list_keys", return_value=keys), \
             mock.patch.object(build.s3store, "get_many_json", return_value=blobs):
            anomalies, _telemetry = build.load_findings(T, NOW)
        return anomalies

    def test_a_finding_below_the_floor_is_dropped_even_though_its_day_is_read(self):
        """The defect itself: same partition as a kept finding, but older."""
        stale = FLOOR - 6 * 3600 * 1000       # 6h below the floor
        fresh = FLOOR + 60 * 1000             # same `dt=`, just inside
        self.assertEqual(_day(stale), _day(fresh),
                         "fixture must put both in one partition to be meaningful")

        kept = self._load([_rec(stale, "stale"), _rec(fresh, "fresh")])

        self.assertEqual([a["anomaly_id"] for a in kept],
                         [a["anomaly_id"] for a in kept if a["ts"] >= FLOOR])
        self.assertEqual(len(kept), 1)
        self.assertGreaterEqual(min(a["ts"] for a in kept), FLOOR)

    def test_a_finding_exactly_on_the_floor_is_inside_the_window(self):
        """Half-open at the bottom, matching `reconcile` and the client's `>=`."""
        self.assertEqual(len(self._load([_rec(FLOOR, "edge")])), 1)

    def test_no_kept_finding_is_ever_below_the_floor(self):
        """The invariant a client can rely on when reproducing `n_anomalies`."""
        spread = [
            _rec(FLOOR - 23 * 3600 * 1000, "a"),
            _rec(FLOOR - 1, "b"),
            _rec(FLOOR, "c"),
            _rec(FLOOR + DAY_MS, "d"),
            _rec(NOW - 3600 * 1000, "e"),
        ]
        kept = self._load(spread)

        self.assertEqual({a["anomaly_id"] for a in kept}, {"c", "d", "e"})
        self.assertFalse([a for a in kept if a["ts"] < FLOOR])


if __name__ == "__main__":
    unittest.main()
