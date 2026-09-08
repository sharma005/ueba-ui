"""`compact_profile` carries the baseline record through untruncated."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from serving import build, config  # noqa: E402


def record(**over) -> dict:
    """A baseline user record shaped like the live schema-v3 objects."""
    u = {
        "user_email": "athul.b@jiostar.com",
        "user_name": "Athul B",
        "first_seen": 1_785_628_762,
        "last_seen": 1_788_145_926,
        "login_count": 90,
        "failed_login_count": 4,
        "ips": {f"10.0.0.{i}": i for i in range(1, 26)},
        "ip_geo": {"10.0.0.1": {"country": "India", "city": "Mumbai",
                                "asn": "Reliance Jio", "lat": 19.07, "lon": 72.88}},
        "countries": {"India": 90},
        "cities": {f"India/City{i}": i for i in range(1, 16)},
        "asns": {"Powergrid": 33, "Reliance Jio": 14},
        "devices": {"macos-chrome": 66, "macos-unknown": 24},
        "apps": {f"SSO_App_{i}": i for i in range(1, 21)},
        "client_apps": {"Browser": 86, "Mobile Apps and Desktop clients": 4},
        "auth_requirements": {"multiFactorAuthentication": 86,
                              "singleFactorAuthentication": 4},
        "hour_histogram": {"8": 24, "9": 16},
        "dow_histogram": {"0": 22, "6": 5},
        "failed_ips": {"10.0.0.1": 1, "10.0.0.2": 3},
        "failed_countries": {"India": 4},
        "distinct_failed_ips": 2,
    }
    u.update(over)
    return u


class CompactProfile(unittest.TestCase):
    def setUp(self):
        self.p = build.compact_profile(record(), lookback_days=30)

    def test_every_baseline_counter_survives(self):
        for field in ("ips", "ip_geo", "countries", "cities", "asns", "devices",
                      "apps", "client_apps", "auth_requirements", "failed_ips",
                      "failed_countries", "hours_hist", "dow_hist"):
            self.assertTrue(self.p[field], f"{field} was dropped")

    def test_maps_are_not_truncated(self):
        # The old builder capped each map at a top 10, which is exactly what the
        # Learned Baseline card must no longer be silently limited by.
        self.assertEqual(len(self.p["ips"]), 25)
        self.assertEqual(len(self.p["cities"]), 15)
        self.assertEqual(len(self.p["apps"]), 20)

    def test_geo_is_carried_per_address(self):
        self.assertEqual(self.p["ip_geo"]["10.0.0.1"]["city"], "Mumbai")

    def test_histograms_are_dense_lists(self):
        self.assertEqual(len(self.p["hours_hist"]), 24)
        self.assertEqual(self.p["hours_hist"][8], 24)
        # Index 0 is Monday, index 6 Sunday.
        self.assertEqual(self.p["dow_hist"], [22, 0, 0, 0, 0, 0, 5])

    def test_scalars_and_derived_counts(self):
        self.assertEqual(self.p["days_seen"], 30)
        self.assertEqual(self.p["login_count"], 90)
        self.assertEqual(self.p["failed_login_count"], 4)
        self.assertEqual(self.p["distinct_ips"], 25)
        self.assertEqual(self.p["distinct_failed_ips"], 2)
        self.assertEqual(self.p["first_seen_ms"], 1_785_628_762_000)
        self.assertEqual(self.p["identifiers"], {"athul.b@jiostar.com": 90})

    def test_missing_fields_degrade_to_empty(self):
        p = build.compact_profile({"user_email": "quiet@jiostar.com"}, lookback_days=30)
        self.assertEqual(p["ips"], {})
        self.assertEqual(p["ip_geo"], {})
        self.assertEqual(p["dow_hist"], [0] * 7)
        self.assertEqual(p["distinct_ips"], 0)


class Sharding(unittest.TestCase):
    def test_shard_count_is_within_the_single_byte_hash(self):
        # `entity_shard` keys off one sha1 byte, so anything above 256 would
        # leave shards permanently empty.
        self.assertLessEqual(config.ENTITY_SHARDS, 256)

    def test_shards_are_spread_across_the_whole_range(self):
        shards = {config.entity_shard(f"user{i}@jiostar.com") for i in range(7000)}
        self.assertEqual(len(shards), config.ENTITY_SHARDS)


if __name__ == "__main__":
    unittest.main()
