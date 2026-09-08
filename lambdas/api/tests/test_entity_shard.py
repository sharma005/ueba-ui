"""The API resolves entity pages against the shard count the snapshot names.

The builder and the API deploy separately. When the API's compiled-in
`ENTITY_SHARDS` ran ahead of the builder's, an account whose sha1 byte was at
least the builder's count resolved to an object no build rewrote: the entity
page served a stale shard left by an earlier run — 0 findings, a lower score —
while the dashboard feed on the same screen showed that account's newest
finding. These pin the resolution rule to `meta.json`.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from serving import config, routes, tenants  # noqa: E402

T = tenants.get("jiostar")
OTHER = tenants.get("deel")

# sha1(...)[0] == 0x79 == 121: 121 % 256 -> "79", 121 % 64 -> "39". The two
# counts disagree for this account, which is what makes it the test case.
ENTITY = "prashant.sarin@viacom18.com"


class ShardResolution(unittest.TestCase):
    def test_count_selects_the_shard(self):
        self.assertEqual(config.entity_shard(ENTITY, 256), "79")
        self.assertEqual(config.entity_shard(ENTITY, 64), "39")

    def test_omitted_count_uses_the_builder_default(self):
        self.assertEqual(
            config.entity_shard(ENTITY), config.entity_shard(ENTITY, config.ENTITY_SHARDS)
        )

    def test_case_and_whitespace_do_not_change_the_shard(self):
        for variant in (f"  {ENTITY}  ", ENTITY.upper()):
            self.assertEqual(config.entity_shard(variant, 64), "39")


class ReaderFollowsSnapshot(unittest.TestCase):
    """`_entity_doc` reads the count from `meta.json`, not from `config`."""

    def setUp(self):
        self.reads: list[tuple[str, str]] = []
        doc = {ENTITY: {"entity": ENTITY, "score": 24, "n_anomalies": 8}}
        stale = {ENTITY: {"entity": ENTITY, "score": 15, "n_anomalies": 7}}
        self.shards = {
            f"{T.snapshot_prefix}/entity/shard-39.json.gz": doc,
            f"{T.snapshot_prefix}/entity/shard-79.json.gz": stale,
        }
        self.meta: dict = {"shards": 64}

        def fake(bucket, key, default=None):
            self.reads.append((bucket, key))
            if key.endswith("/meta.json"):
                return self.meta
            return self.shards.get(key, default)

        self.orig = routes.s3store.get_json_cached
        routes.s3store.get_json_cached = fake

    def tearDown(self):
        routes.s3store.get_json_cached = self.orig

    def test_reads_the_shard_the_snapshot_was_built_with(self):
        self.assertEqual(routes._entity_doc(T, ENTITY)["n_anomalies"], 8)
        keys = [k for _, k in self.reads]
        self.assertIn(f"{T.snapshot_prefix}/entity/shard-39.json.gz", keys)
        self.assertNotIn(f"{T.snapshot_prefix}/entity/shard-79.json.gz", keys)

    def test_follows_the_builder_when_the_snapshot_catches_up(self):
        self.meta = {"shards": 256}
        self.assertEqual(routes._entity_doc(T, ENTITY)["n_anomalies"], 7)

    def test_falls_back_to_config_when_meta_omits_the_count(self):
        for meta in ({}, {"shards": 0}, {"shards": "64"}):
            with self.subTest(meta=meta):
                self.meta = meta
                self.assertIsNone(routes._snapshot_shards(T))

    def test_health_names_both_counts(self):
        self.meta = {"shards": 64, "generated_at_ms": 0}
        h = routes.health(T, {})
        self.assertEqual(h["entity_shards"], 64)
        self.assertEqual(h["entity_shards_expected"], config.ENTITY_SHARDS)

    def test_the_same_entity_resolves_into_a_different_bucket_per_tenant(self):
        """The shard number is tenant-independent; the object it names is not.

        This is the property that keeps two customers' identically-named
        accounts from sharing a document: same shard, different bucket *and*
        different prefix.
        """
        routes._entity_doc(OTHER, ENTITY)
        buckets = {b for b, _ in self.reads}
        self.assertEqual(buckets, {OTHER.bucket})
        self.assertTrue(all(k.startswith(OTHER.state_prefix + "/") for _, k in self.reads))
        self.assertNotEqual(T.bucket, OTHER.bucket)


if __name__ == "__main__":
    unittest.main()
