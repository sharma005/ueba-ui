"""Which tenant a snapshot invocation builds.

The S3 trigger fires on every detector's `baseline.json`, in whichever bucket.
Getting this wrong is not a 500 — it is one customer's detector run silently
rewriting another customer's snapshot, which no alarm anywhere would catch.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import snapshot  # noqa: E402
from serving import tenants  # noqa: E402

JIO = tenants.get("jiostar")
DEEL = tenants.get("deel")


def s3_event(*pairs):
    return {"Records": [{"s3": {"bucket": {"name": b}, "object": {"key": k}}}
                        for b, k in pairs]}


class FromS3Event(unittest.TestCase):
    def test_each_tenants_baseline_resolves_to_that_tenant(self):
        for t in (JIO, DEEL):
            with self.subTest(tenant=t.id):
                got = snapshot._tenants_from_event(s3_event((t.bucket, t.baseline_key)))
                self.assertEqual([x.id for x in got], [t.id])

    def test_right_bucket_wrong_prefix_builds_nothing(self):
        """Never fall through to the default — that is the cross-tenant write."""
        got = snapshot._tenants_from_event(
            s3_event((JIO.bucket, "tenants/somebodyelse/baseline/baseline.json")))
        self.assertEqual(got, [])

    def test_unknown_bucket_builds_nothing(self):
        got = snapshot._tenants_from_event(
            s3_event(("some-unrelated-bucket", "tenants/jiostar/baseline/baseline.json")))
        self.assertEqual(got, [])

    def test_a_mixed_batch_still_builds_the_records_it_recognises(self):
        got = snapshot._tenants_from_event(s3_event(
            ("nope", "tenants/jiostar/baseline/baseline.json"),
            (DEEL.bucket, DEEL.baseline_key),
        ))
        self.assertEqual([x.id for x in got], ["deel"])

    def test_repeated_records_build_once(self):
        got = snapshot._tenants_from_event(s3_event(
            (JIO.bucket, JIO.baseline_key),
            (JIO.bucket, JIO.detector_state_key),
        ))
        self.assertEqual([x.id for x in got], ["jiostar"])


class FromManualPayload(unittest.TestCase):
    def test_named_tenant(self):
        got = snapshot._tenants_from_event({"tenant": "deel"})
        self.assertEqual([x.id for x in got], ["deel"])

    def test_named_list_keeps_the_given_order(self):
        got = snapshot._tenants_from_event({"tenants": ["deel", "jiostar"]})
        self.assertEqual([x.id for x in got], ["deel", "jiostar"])

    def test_all_returns_registry_order(self):
        got = snapshot._tenants_from_event({"all": True})
        self.assertEqual([x.id for x in got], ["jiostar", "deel"])

    def test_empty_payload_preserves_the_old_behaviour(self):
        """`aws lambda invoke` with `{}` built JioStar before, and still does."""
        for event in ({}, None):
            with self.subTest(event=event):
                got = snapshot._tenants_from_event(event)
                self.assertEqual([x.id for x in got], ["jiostar"])

    def test_unknown_named_tenant_raises(self):
        with self.assertRaises(tenants.UnknownTenant):
            snapshot._tenants_from_event({"tenant": "acme"})


class BuildIsolation(unittest.TestCase):
    """One tenant's failure must not cost another's build."""

    def _run(self, targets, failing=()):
        calls = []

        def fake_build(t, now_ms=None):
            calls.append(t.id)
            if t.id in failing:
                raise RuntimeError("boom")
            return {"entities": 1, "baseline": {"secret": "should be stripped"}}

        import serving.build

        orig = serving.build.build
        serving.build.build = fake_build
        try:
            return snapshot._build_each(targets), calls
        finally:
            serving.build.build = orig

    def test_each_tenant_is_built_in_order(self):
        out, calls = self._run([JIO, DEEL])
        self.assertEqual(calls, ["jiostar", "deel"])
        self.assertEqual(set(out["results"]), {"jiostar", "deel"})
        self.assertNotIn("errors", out)

    def test_a_failure_is_reported_without_losing_the_other(self):
        out, calls = self._run([JIO, DEEL], failing={"jiostar"})
        self.assertEqual(calls, ["jiostar", "deel"], "the second build was skipped")
        self.assertEqual(list(out["results"]), ["deel"])
        self.assertIn("RuntimeError", out["errors"]["jiostar"])

    def test_the_baseline_block_is_stripped_from_the_reply(self):
        out, _ = self._run([JIO])
        self.assertNotIn("baseline", out["results"]["jiostar"])


class HandlerShape(unittest.TestCase):
    def test_single_tenant_keeps_the_flat_reply(self):
        """So the existing `aws lambda invoke ... /dev/stdout` reads unchanged."""
        import serving.build

        orig = serving.build.build
        serving.build.build = lambda t, now_ms=None: {"entities": 3, "baseline": {}}
        try:
            out = snapshot.handler({"tenant": "deel"}, None)
        finally:
            serving.build.build = orig
        self.assertEqual(out, {"entities": 3})

    def test_an_unmatched_event_says_so_rather_than_building(self):
        out = snapshot.handler(s3_event(("nope", "nope")), None)
        self.assertEqual(out["results"], {})
        self.assertIn("no tenant matched", out["note"])


if __name__ == "__main__":
    unittest.main()
