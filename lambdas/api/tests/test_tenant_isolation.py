"""No request may ever touch another tenant's storage.

This is the test the whole multi-tenant refactor exists to make possible. It
records the `(bucket, key)` of every S3 read a request performs and asserts all
of them belong to the tenant that was asked for. With the bucket threaded
explicitly, a mix-up is visible here; when `s3store` read a module-level
`config.BUCKET`, the same assertion could only have checked the key, and a
wrong-bucket read would have been invisible.

It is driven off `routes._ROUTES` rather than a hand-written list, so a route
added later without tenant plumbing fails automatically instead of quietly
going unchecked.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from serving import routes, tenants  # noqa: E402

JIO = tenants.get("jiostar")
DEEL = tenants.get("deel")
ENTITY = "someone@example.com"

# One representative request per route, in the shape `handle` receives.
REQUESTS = [
    ("/api/health", {}),
    ("/api/overview", {}),
    ("/api/notables", {}),
    ("/api/entities", {"q": "a"}),
    ("/api/anomalies", {"days": "7", "limit": "10"}),
    ("/api/correlations", {}),
    ("/api/tuning", {}),
    (f"/api/entities/{ENTITY}", {}),
    (f"/api/entities/{ENTITY}/graph", {}),
    (f"/api/entities/{ENTITY}/events", {"hours": "24"}),
]

# Routes that legitimately touch no storage. `/tuning` serves `derive`'s and
# `catalog`'s in-process constants — the same scoring ladder for every customer
# — so it has nothing to read and nothing to leak. Named explicitly rather than
# softening the "must read something" assertion, which is what catches a route
# that stopped being tenant-scoped by accident.
NO_STORAGE = {"/api/tuning"}


class RecordingStore:
    """Stands in for `s3store`, logging every access and serving plausible docs."""

    def __init__(self):
        self.reads: list[tuple[str, str]] = []

    def _doc(self, key: str):
        if key.endswith("/meta.json"):
            return {"shards": 256, "generated_at_ms": 1, "entities": 1}
        if key.endswith(".json.gz") and "/entity/" in key:
            return {ENTITY: {"entity": ENTITY, "score": 1, "profile": {},
                             "anomalies": []}}
        if key.endswith("/feed.json.gz"):
            return {"anomalies": [], "per_day": {}}
        return {}

    def get_json_cached(self, bucket, key, default=None):
        self.reads.append((bucket, key))
        return self._doc(key)

    def get_json(self, bucket, key, default=None):
        self.reads.append((bucket, key))
        return self._doc(key)


class Isolation(unittest.TestCase):
    def setUp(self):
        self.store = RecordingStore()
        self.orig = routes.s3store
        routes.s3store = self.store

    def tearDown(self):
        routes.s3store = self.orig

    def _drive(self, path, query, tenant_id):
        self.store.reads.clear()
        q = dict(query)
        if tenant_id is not None:
            q["tenant"] = tenant_id
        status, body = routes.handle("GET", path, q, None)
        return status, body

    def test_every_route_reads_only_the_requested_tenant(self):
        for want in (JIO, DEEL):
            for path, query in REQUESTS:
                with self.subTest(tenant=want.id, path=path):
                    status, body = self._drive(path, query, want.id)
                    self.assertEqual(status, 200, body)
                    if path in NO_STORAGE:
                        self.assertEqual(self.store.reads, [],
                                         f"{path} is declared storage-free but "
                                         f"read S3")
                        continue
                    self.assertTrue(self.store.reads,
                                    f"{path} read nothing — is it actually "
                                    f"tenant-scoped?")
                    for bucket, key in self.store.reads:
                        self.assertEqual(bucket, want.bucket, f"{path} -> {key}")
                        self.assertTrue(
                            key.startswith(want.state_prefix + "/"),
                            f"{path} read {key!r}, outside {want.state_prefix!r}")

    def test_every_declared_route_is_covered(self):
        """Keeps this file honest as routes are added."""
        import re

        covered = set()
        for path, _ in REQUESTS:
            covered.add(path[len("/api"):])
        for _meth, pattern, _fn in routes._ROUTES:
            if pattern == r"^/tenants$":
                continue  # deliberately tenant-free; see TenantFreeRoute below
            rx = re.compile(pattern)
            self.assertTrue(
                any(rx.match(p) for p in covered),
                f"route {pattern} has no request in REQUESTS — add one so its "
                f"tenant scoping is checked")

    def test_an_entity_in_one_tenant_is_absent_from_the_other(self):
        """The inverse: asking the wrong tenant 404s and never touches the right one."""
        only_jio = {ENTITY: {"entity": ENTITY, "score": 9, "profile": {},
                             "anomalies": []}}

        def doc(bucket, key, default=None):
            self.store.reads.append((bucket, key))
            if key.endswith("/meta.json"):
                return {"shards": 256}
            if "/entity/" in key and bucket == JIO.bucket:
                return only_jio
            return {}

        self.store.get_json_cached = doc
        self.store.reads.clear()
        status, _ = routes.handle("GET", f"/api/entities/{ENTITY}",
                                  {"tenant": DEEL.id}, None)
        self.assertEqual(status, 404)
        self.assertTrue(self.store.reads)
        self.assertNotIn(JIO.bucket, {b for b, _ in self.store.reads})


class TenantResolution(unittest.TestCase):
    def setUp(self):
        self.store = RecordingStore()
        self.orig = routes.s3store
        routes.s3store = self.store

    def tearDown(self):
        routes.s3store = self.orig

    def test_absent_tenant_falls_back_to_the_default(self):
        status, body = routes.handle("GET", "/api/health", {}, None)
        self.assertEqual(status, 200)
        self.assertEqual(body["tenant"], "jiostar")
        self.assertEqual({b for b, _ in self.store.reads}, {JIO.bucket})

    def test_health_names_the_tenant_that_answered(self):
        _, body = routes.handle("GET", "/api/health", {"tenant": "deel"}, None)
        self.assertEqual(body["tenant"], "deel")
        self.assertEqual(body["tenant_label"], "Deel")

    def test_unknown_tenant_is_a_400_not_a_silent_fallback(self):
        """Serving a different customer than the one named is the worst outcome."""
        status, body = routes.handle("GET", "/api/health", {"tenant": "acme"}, None)
        self.assertEqual(status, 400)
        self.assertIn("acme", body["error"])
        self.assertEqual(body["known"], ["jiostar", "deel"])
        self.assertEqual(self.store.reads, [], "a rejected request read S3")

    def test_blank_tenant_is_treated_as_absent(self):
        status, body = routes.handle("GET", "/api/health", {"tenant": "  "}, None)
        self.assertEqual(status, 200)
        self.assertEqual(body["tenant"], "jiostar")


class TenantFreeRoute(unittest.TestCase):
    def setUp(self):
        self.store = RecordingStore()
        self.orig = routes.s3store
        routes.s3store = self.store

    def tearDown(self):
        routes.s3store = self.orig

    def test_tenants_needs_no_parameter(self):
        status, body = routes.handle("GET", "/api/tenants", {}, None)
        self.assertEqual(status, 200)
        self.assertEqual(body["default"], "jiostar")
        self.assertEqual([t["id"] for t in body["tenants"]], ["jiostar", "deel"])

    def test_tenants_reports_readiness_from_the_snapshot(self):
        def only_jio_has_a_snapshot(bucket, key, default=None):
            self.store.reads.append((bucket, key))
            if bucket == JIO.bucket:
                return {"generated_at": "now", "generated_at_ms": 1, "entities": 7}
            return None

        self.store.get_json_cached = only_jio_has_a_snapshot
        _, body = routes.handle("GET", "/api/tenants", {}, None)
        rows = {t["id"]: t for t in body["tenants"]}
        self.assertTrue(rows["jiostar"]["ready"])
        self.assertEqual(rows["jiostar"]["entities"], 7)
        self.assertFalse(rows["deel"]["ready"])
        self.assertIsNone(rows["deel"]["snapshot_generated_at"])

    def test_one_unreadable_tenant_does_not_500_the_list(self):
        def deel_denied(bucket, key, default=None):
            if bucket == DEEL.bucket:
                raise PermissionError("AccessDenied")
            return {"generated_at_ms": 1}

        self.store.get_json_cached = deel_denied
        status, body = routes.handle("GET", "/api/tenants", {}, None)
        self.assertEqual(status, 200)
        rows = {t["id"]: t for t in body["tenants"]}
        self.assertTrue(rows["jiostar"]["ready"])
        self.assertFalse(rows["deel"]["ready"])
        self.assertIn("AccessDenied", rows["deel"]["error"])

    def test_no_infrastructure_in_the_response(self):
        import json

        _, body = routes.handle("GET", "/api/tenants", {}, None)
        blob = json.dumps(body)
        for t in tenants.all():
            self.assertNotIn(t.bucket, blob)
            self.assertNotIn(t.state_prefix, blob)


class AuthPrecedesTenant(unittest.TestCase):
    """403 before 400: an unauthenticated caller must not enumerate tenant ids."""

    def setUp(self):
        self.orig_key = routes.config.API_KEY
        routes.config.API_KEY = "secret"

    def tearDown(self):
        routes.config.API_KEY = self.orig_key

    def test_bad_key_with_bad_tenant_is_403(self):
        status, body = routes.handle("GET", "/api/health", {"tenant": "acme"}, None,
                                     headers={})
        self.assertEqual(status, 403)
        self.assertNotIn("known", body)

    def test_bad_key_on_the_tenant_list_is_403(self):
        status, _ = routes.handle("GET", "/api/tenants", {}, None, headers={})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
