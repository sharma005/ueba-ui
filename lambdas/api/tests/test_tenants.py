"""The registry, and the exact key strings it must keep producing.

The golden test below is the one that proves the multi-tenant refactor did not
move JioStar's data. Every key `config.py` used to compute at import time is
pinned here as a literal: a one-character drift in `snapshot_prefix` repoints a
whole customer at objects nothing writes, and every other test in this suite
would still pass.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from serving import config, tenants  # noqa: E402


class GoldenKeys(unittest.TestCase):
    """Literal strings, not re-derived from the tenant. That is the point."""

    def setUp(self):
        self.t = tenants.get("jiostar")

    def test_bucket_and_prefix(self):
        self.assertEqual(self.t.bucket,
                         "azuread-anomaly-iforest-state-119418761367-ap2")
        self.assertEqual(self.t.state_prefix, "tenants/jiostar")

    def test_every_derived_key(self):
        t = self.t
        self.assertEqual(t.baseline_key, "tenants/jiostar/baseline/baseline.json")
        self.assertEqual(t.detector_state_key,
                         "tenants/jiostar/baseline/detector_state.json")
        self.assertEqual(t.ui_prefix, "tenants/jiostar/ui")
        self.assertEqual(t.anomaly_prefix, "tenants/jiostar/ui/anomalies")
        self.assertEqual(t.snapshot_prefix, "tenants/jiostar/ui/snapshot/v1")
        self.assertEqual(t.feed_key, "tenants/jiostar/ui/snapshot/v1/feed.json.gz")
        self.assertEqual(t.snapshot_key("meta.json"),
                         "tenants/jiostar/ui/snapshot/v1/meta.json")

    def test_entity_shard_key(self):
        # sha1(...)[0] == 0x79 -> "79" at 256 shards. Same account as
        # test_entity_shard.py, so the two files pin the same rule.
        self.assertEqual(
            self.t.entity_shard_key("prashant.sarin@viacom18.com"),
            "tenants/jiostar/ui/snapshot/v1/entity/shard-79.json.gz",
        )
        self.assertEqual(
            self.t.entity_shard_key("prashant.sarin@viacom18.com", 64),
            "tenants/jiostar/ui/snapshot/v1/entity/shard-39.json.gz",
        )

    def test_deel_keys_are_disjoint_from_jiostar(self):
        d = tenants.get("deel")
        self.assertEqual(d.bucket, "okta-anomaly-iforest-state-119418761367-eu1")
        self.assertEqual(d.snapshot_prefix, "tenants/deel/ui/snapshot/v1")
        self.assertNotEqual(d.bucket, self.t.bucket)
        self.assertFalse(d.state_prefix.startswith(self.t.state_prefix))
        self.assertFalse(self.t.state_prefix.startswith(d.state_prefix))


class Lookup(unittest.TestCase):
    def test_unknown_raises(self):
        with self.assertRaises(tenants.UnknownTenant):
            tenants.get("nope")

    def test_default_is_jiostar(self):
        """Every caller predating `?tenant=` was already served this one."""
        self.assertEqual(tenants.default().id, "jiostar")

    def test_registry_order_is_dropdown_order(self):
        self.assertEqual(tenants.ids(), ["jiostar", "deel"])

    def test_all_returns_a_copy(self):
        got = tenants.all()
        got.append("junk")
        self.assertEqual(len(tenants.all()), 2)


class Ownership(unittest.TestCase):
    """`owns` and `resolve_s3` gate the snapshot builder. Both halves matter."""

    def setUp(self):
        self.jio = tenants.get("jiostar")
        self.deel = tenants.get("deel")

    def test_right_bucket_and_prefix(self):
        self.assertTrue(self.jio.owns(self.jio.bucket,
                                      "tenants/jiostar/baseline/baseline.json"))

    def test_right_bucket_wrong_prefix(self):
        """A bucket can hold more than one tenant, so the prefix must be checked."""
        self.assertFalse(self.jio.owns(self.jio.bucket,
                                       "tenants/somebodyelse/baseline/baseline.json"))

    def test_wrong_bucket_right_prefix(self):
        """And two tenants could use the same prefix in different buckets."""
        self.assertFalse(self.jio.owns(self.deel.bucket,
                                       "tenants/jiostar/baseline/baseline.json"))

    def test_prefix_must_end_at_a_boundary(self):
        """`tenants/jiostar2` must not be claimed by `tenants/jiostar`."""
        self.assertFalse(
            self.jio.owns(self.jio.bucket, "tenants/jiostar2/baseline/baseline.json"))

    def test_resolve_s3_finds_each_tenant(self):
        self.assertEqual(
            tenants.resolve_s3(self.deel.bucket, self.deel.baseline_key).id, "deel")
        self.assertEqual(
            tenants.resolve_s3(self.jio.bucket, self.jio.baseline_key).id, "jiostar")

    def test_resolve_s3_returns_none_rather_than_defaulting(self):
        """The property that stops one customer's event rebuilding another's."""
        self.assertIsNone(tenants.resolve_s3("some-other-bucket", "tenants/jiostar/x"))
        self.assertIsNone(tenants.resolve_s3(self.jio.bucket, "unclaimed/prefix/x"))


class PublicContract(unittest.TestCase):
    def test_infrastructure_never_leaves_the_process(self):
        for t in tenants.all():
            row = t.public()
            self.assertEqual(set(row), {"id", "label", "product", "source"})
            blob = json.dumps(row)
            self.assertNotIn(t.bucket, blob)
            self.assertNotIn(t.state_prefix, blob)
            if t.coralogix_secret_arn:
                self.assertNotIn(t.coralogix_secret_arn, blob)


class EnvOverride(unittest.TestCase):
    """`UEBA_TENANTS` is how a deployment repoints a tenant without a release."""

    @staticmethod
    def _reload(value):
        import importlib

        with mock.patch.dict(os.environ, {"UEBA_TENANTS": value}, clear=False):
            return importlib.reload(tenants)

    def tearDown(self):
        import importlib

        os.environ.pop("UEBA_TENANTS", None)
        importlib.reload(tenants)

    def test_patch_merges_by_id(self):
        m = self._reload(json.dumps([{"id": "deel", "label": "Deel Inc."}]))
        self.assertEqual(m.get("deel").label, "Deel Inc.")
        # Untouched fields survive the patch.
        self.assertEqual(m.get("deel").bucket,
                         "okta-anomaly-iforest-state-119418761367-eu1")
        self.assertEqual(len(m.all()), 2)

    def test_new_tenant_is_appended(self):
        m = self._reload(json.dumps([{
            "id": "acme", "label": "Acme", "product": "Okta",
            "source_label": "Okta System Log",
            "bucket": "acme-state", "state_prefix": "tenants/acme",
        }]))
        self.assertEqual(m.ids(), ["jiostar", "deel", "acme"])
        self.assertEqual(m.get("acme").snapshot_prefix, "tenants/acme/ui/snapshot/v1")

    def test_malformed_value_is_ignored_not_fatal(self):
        """A bad environment variable must not take the API down."""
        for bad in ("not json", '{"id": "x"}', '[{"no_id": 1}]', '[{"id":"z"}]'):
            with self.subTest(bad=bad):
                m = self._reload(bad)
                self.assertEqual(m.ids()[:2], ["jiostar", "deel"])


class StaysDeploymentWide(unittest.TestCase):
    def test_entity_shard_is_tenant_independent(self):
        """The shard is a function of the name; the tenant is in the prefix."""
        e = "prashant.sarin@viacom18.com"
        self.assertEqual(config.entity_shard(e), "79")
        self.assertNotIn("tenant", config.entity_shard.__doc__.lower().split()[:5])

    def test_tenant_constants_are_gone_from_config(self):
        """A caller that forgets the tenant must fail, not silently get JioStar."""
        for gone in ("BUCKET", "TENANT_ID", "STATE_PREFIX", "BASELINE_KEY",
                     "SNAPSHOT_PREFIX", "ANOMALY_PREFIX", "FEED_KEY",
                     "UI_PREFIX", "DETECTOR_STATE_KEY", "CORALOGIX_SECRET_ARN"):
            self.assertFalse(hasattr(config, gone),
                             f"config.{gone} still exists — it must live on Tenant")


if __name__ == "__main__":
    unittest.main()
