"""Scoring and chaining behaviour."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from serving import derive  # noqa: E402

NOW = 1_788_000_000_000


def anom(weight: int, age_days: float = 0.0, detection: str = "impossible_travel",
         ident: str = "a", entity: str = "u@x.com", ip: str | None = None) -> dict:
    return {
        "anomaly_id": ident,
        "ts": NOW - int(age_days * derive.DAY_MS),
        "weight": weight,
        "detection_id": detection,
        "name": detection,
        "entity": entity,
        "evidence": {"ip": ip} if ip else {},
    }


CRITICAL_KINDS = ["impossible_travel", "mfa_fatigue", "credential_attack_success",
                  "session_reuse_new_device"]


class TestScore(unittest.TestCase):
    def test_bands_are_reachable_by_distinct_findings(self):
        """The calibration claim in derive.py, asserted."""
        one = [anom(30, 0, CRITICAL_KINDS[0], "a")]
        self.assertEqual(derive.band_of(derive.score_at(one, NOW)), "medium")
        two = [anom(30, 0, CRITICAL_KINDS[0], "a"), anom(30, 0.1, CRITICAL_KINDS[1], "b")]
        self.assertEqual(derive.band_of(derive.score_at(two, NOW)), "high")
        four = [anom(30, i * 0.1, k, str(i)) for i, k in enumerate(CRITICAL_KINDS)]
        self.assertEqual(derive.band_of(derive.score_at(four, NOW)), "notable")

    def test_half_life(self):
        fresh = derive.decayed_points([anom(30)], NOW)
        aged = derive.decayed_points([anom(30, derive.HALF_LIFE_DAYS)], NOW)
        self.assertAlmostEqual(aged, fresh / 2, places=6)

    def test_points_are_grouped_by_detection(self):
        mixed = [anom(20, 0, "legacy_auth", "a"), anom(20, 0, "legacy_auth", "b"),
                 anom(20, 0, "new_country", "c")]
        by_kind = derive.decayed_points_by_kind(mixed, NOW)
        self.assertAlmostEqual(by_kind["legacy_auth"], 40.0, places=6)
        self.assertAlmostEqual(by_kind["new_country"], 20.0, places=6)

    def test_outside_window_is_dropped(self):
        self.assertEqual(derive.score_at([anom(38, derive.WINDOW_DAYS + 1)], NOW), 0)

    def test_future_anomalies_are_ignored(self):
        """`score_at` is used to reconstruct history; it must not see ahead."""
        self.assertEqual(derive.score_at([anom(30, -5)], NOW), 0)

    def test_score_never_exceeds_100(self):
        many = [anom(38, 0, CRITICAL_KINDS[i % 4], str(i)) for i in range(200)]
        self.assertLessEqual(derive.score_at(many, NOW), 100)

    def test_repeating_one_detection_cannot_reach_notable(self):
        """The `legal.clms` case: 34 daily legacy_auth findings and nothing else.

        A service account doing the same benign thing on schedule must not
        outrank an account under a real credential attack.
        """
        for n in (4, 12, 34, 120):
            repeats = [anom(21, i * 0.8, "legacy_auth", str(i)) for i in range(n)]
            score = derive.score_at(repeats, NOW)
            self.assertLess(score, derive.BANDS["notable"],
                            f"{n} repeats of one kind reached {score}")

    def test_breadth_outranks_repetition(self):
        repeats = [anom(21, i * 0.5, "legacy_auth", str(i)) for i in range(30)]
        spread = [anom(21, i * 0.5, k, f"s{i}")
                  for i, k in enumerate(CRITICAL_KINDS + ["legacy_auth", "new_country"])]
        self.assertGreater(derive.score_at(spread, NOW), derive.score_at(repeats, NOW))

    def test_one_kind_cannot_exceed_its_ceiling(self):
        endless = [anom(38, 0, "legacy_auth", str(i)) for i in range(500)]
        self.assertLessEqual(derive.combined_points(endless, NOW), derive.KIND_CEILING + 1e-9)

    def test_empty_is_zero_not_an_error(self):
        self.assertEqual(derive.score_at([], NOW), 0)


class TestHistory(unittest.TestCase):
    def test_history_is_oldest_first_and_ends_today(self):
        h = derive.history([anom(30, 3)], NOW, 5)
        self.assertEqual(len(h), 5)
        self.assertLess(h[0]["dt"], h[-1]["dt"])
        self.assertEqual(h[-1]["dt"], derive._day_str(NOW))

    def test_score_rises_when_the_finding_lands(self):
        h = derive.history([anom(30, 2)], NOW, 5)
        self.assertEqual(h[0]["score"], 0)
        self.assertGreater(h[-1]["score"], 0)


class TestChains(unittest.TestCase):
    def test_needs_two_distinct_tactics(self):
        """Nine of the same signal is a noisy account, not a chain."""
        same = [anom(12, i * 0.1, "unusual_hour", ident=str(i)) for i in range(9)]
        self.assertEqual(derive.chains({"u@x.com": same}, NOW), [])

    def test_chain_score_is_not_inflated_by_repetition(self):
        """Eight repeats of one signal plus one other is not a maximal incident."""
        anoms = [anom(21, 0.5 + i * 0.01, "legacy_auth", f"l{i}") for i in range(8)]
        anoms.append(anom(21, 0.2, "credential_attack_volume", "c"))
        out = derive.chains({"u@x.com": anoms}, NOW)
        self.assertEqual(len(out), 1)
        self.assertLess(out[0]["combined_score"], 100)

    def test_two_tactics_make_a_chain(self):
        anoms = [
            anom(21, 0.5, "credential_attack_volume", ident="a"),
            anom(30, 0.2, "impossible_travel", ident="b"),
        ]
        out = derive.chains({"u@x.com": anoms}, NOW)
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual(c["n_anomalies"], 2)
        self.assertEqual(c["entities"], ["user:u@x.com"])
        self.assertEqual(c["entity_count"], 1)
        self.assertEqual(len(c["tactic_sequence"]), 2)
        self.assertEqual(c["origin_mix"], {"native": 2})
        self.assertEqual(sum(c["detection_mix"].values()), 2)
        self.assertTrue(all(s["origin"] == "native" for s in c["steps"]))

    def test_a_long_gap_splits_a_chain(self):
        anoms = [
            anom(21, 20, "credential_attack_volume", ident="a"),
            anom(30, 19.9, "impossible_travel", ident="b"),
            anom(21, 1, "credential_attack_volume", ident="c"),
            anom(30, 0.9, "impossible_travel", ident="d"),
        ]
        self.assertEqual(len(derive.chains({"u@x.com": anoms}, NOW)), 2)

    def test_shared_ip_merges_accounts(self):
        """A spray is one incident across many accounts, not many incidents."""
        by = {
            "a@x.com": [anom(21, 0.5, "credential_attack_volume", ident="a1", entity="a@x.com", ip="5.5.5.5"),
                        anom(30, 0.4, "impossible_travel", ident="a2", entity="a@x.com", ip="5.5.5.5")],
            "b@x.com": [anom(21, 0.3, "credential_attack_volume", ident="b1", entity="b@x.com", ip="5.5.5.5"),
                        anom(30, 0.2, "impossible_travel", ident="b2", entity="b@x.com", ip="5.5.5.5")],
        }
        out = derive.chains(by, NOW)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["entity_count"], 2)
        self.assertEqual(out[0]["n_anomalies"], 4)
        self.assertEqual(len(out[0]["anomaly_ids"]), len(set(out[0]["anomaly_ids"])))

    def test_unrelated_accounts_stay_separate(self):
        by = {
            "a@x.com": [anom(21, 0.5, "credential_attack_volume", ident="a1", entity="a@x.com", ip="1.1.1.1"),
                        anom(30, 0.4, "impossible_travel", ident="a2", entity="a@x.com", ip="1.1.1.1")],
            "b@x.com": [anom(21, 0.3, "credential_attack_volume", ident="b1", entity="b@x.com", ip="2.2.2.2"),
                        anom(30, 0.2, "impossible_travel", ident="b2", entity="b@x.com", ip="2.2.2.2")],
        }
        self.assertEqual(len(derive.chains(by, NOW)), 2)


class TestEntityRows(unittest.TestCase):
    def test_delta_reflects_the_last_day(self):
        by = {"u@x.com": [anom(30, 0.1, ident="new")]}
        row = derive.entity_rows(by, NOW)["u@x.com"]
        self.assertGreater(row["delta_24h"], 0)
        self.assertEqual(row["n_anomalies_24h"], 1)

    def test_quiet_account_has_no_delta(self):
        by = {"u@x.com": [anom(30, 15, ident="old")]}
        row = derive.entity_rows(by, NOW)["u@x.com"]
        self.assertLessEqual(row["delta_24h"], 0)
        self.assertEqual(row["n_anomalies_24h"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
