"""Invariants the UI depends on but cannot check for itself."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from serving import catalog, derive, links, normalize, tenants  # noqa: E402

T = tenants.get("jiostar")
# `normalize.anomaly` builds Coralogix deep links, which are per tenant. These
# tests are about classification, not links, so they pass the tenant's own
# config once here rather than repeating it at thirty call sites. With no
# `CORALOGIX_UI_BASE` set the URLs come out None, which is what the assertions
# below already expect.
_LINKS = links.for_tenant(T)


def anomaly_(record):
    return normalize.anomaly(record, _LINKS)


def weight_to_sev(w: float) -> str:
    """`weightToSev` from ui/src/theme.ts, transcribed.

    If this ever drifts from the TypeScript, the Anomaly Feed's severity filter
    starts disagreeing with the detector, so it is pinned here on purpose.
    """
    if w >= 26:
        return "critical"
    if w >= 18:
        return "error"
    if w >= 10:
        return "warning"
    return "info"


class TestWeights(unittest.TestCase):
    def test_weight_matches_severity_for_every_kind(self):
        """The UI colours by weight; the detector decides severity. They must agree."""
        for kind in catalog.CATALOG:
            for sev in ("critical", "error", "warning", "info"):
                w = catalog.weight_for(kind, sev)
                self.assertEqual(
                    weight_to_sev(w), sev,
                    f"{kind} at {sev} scored {w}, which the UI paints as {weight_to_sev(w)}",
                )

    def test_unknown_severity_falls_back_to_info(self):
        w = catalog.weight_for("impossible_travel", "not-a-severity")
        self.assertEqual(weight_to_sev(w), "info")

    def test_unknown_kind_is_still_weighted(self):
        self.assertEqual(weight_to_sev(catalog.weight_for("nonesuch", "critical")), "critical")


class TestPrimaryKind(unittest.TestCase):
    def test_priority_beats_list_order(self):
        """The detector appends reasons in evaluation order, cheapest first."""
        rec = {"reasons": [{"kind": "unusual_hour"}, {"kind": "impossible_travel"}]}
        self.assertEqual(catalog.primary_kind(rec), "impossible_travel")

    def test_no_baseline_only_wins_alone(self):
        self.assertEqual(catalog.primary_kind({"reasons": [{"kind": "no_baseline"}]}),
                         "no_baseline")
        self.assertEqual(
            catalog.primary_kind({"reasons": [{"kind": "no_baseline"}, {"kind": "new_country"}]}),
            "new_country")

    def test_generic_deviation_kind_is_not_mislabelled(self):
        """An unrecognised reason must read as unclassified, not as a real detection."""
        rec = {"detection_kind": "azure_ad_credential_abuse_deviation",
               "reasons": [{"kind": "some_future_signal"}]}
        self.assertEqual(catalog.primary_kind(rec), "unknown")

    def test_every_tactic_has_a_rank(self):
        for d in catalog.CATALOG.values():
            self.assertIn(d.tactic, catalog.TACTIC_RANKS, f"{d.kind} -> {d.tactic}")

    def test_every_fallback_names_a_kind_that_exists(self):
        for raw, kind in catalog.DETECTION_KIND_FALLBACK.items():
            self.assertIn(kind, catalog.CATALOG, f"{raw} -> {kind}")

    def test_both_vendors_generic_kinds_stay_out_of_the_fallback(self):
        """Mapping them to any one detection would mislabel every finding."""
        for generic in ("azure_ad_credential_abuse_deviation",
                        "okta_credential_abuse_deviation"):
            self.assertNotIn(generic, catalog.DETECTION_KIND_FALLBACK)
            self.assertEqual(
                catalog.primary_kind({"detection_kind": generic, "reasons": []}),
                "unknown")

    def test_okta_detection_kinds_resolve(self):
        for raw, want in (("okta_mfa_fatigue", "mfa_fatigue"),
                          ("okta_spray_infrastructure", "spray_infrastructure"),
                          ("okta_credential_attack_volume", "credential_attack_volume"),
                          ("okta_credential_attack_success", "credential_attack_success")):
            with self.subTest(raw=raw):
                self.assertEqual(
                    catalog.primary_kind({"detection_kind": raw, "reasons": []}), want)

    def test_the_new_okta_reason_kinds_are_classified(self):
        for kind in ("okta_risk", "anonymizing_proxy"):
            with self.subTest(kind=kind):
                self.assertIn(kind, catalog.CATALOG)
                self.assertNotEqual(catalog.get(kind), catalog.UNKNOWN)
                self.assertEqual(
                    catalog.primary_kind({"reasons": [{"kind": kind}]}), kind)

    def test_priority_order_of_the_pre_existing_kinds_is_unchanged(self):
        """Adding a kind must not reshuffle how JioStar's findings are labelled.

        `_PRIORITY` is dict insertion order and decides `primary_kind` for a
        multi-reason finding. Inserting `okta_risk` and `anonymizing_proxy` into
        the middle shifts every index after them — harmless, because
        `primary_kind` takes a `min` over only the kinds a record carries, and
        no Azure finding carries either. This pins that reasoning: the relative
        order of everything that was already here must hold.
        """
        before = [
            "credential_attack_success", "impossible_travel",
            "session_reuse_new_device", "mfa_fatigue", "concurrent_geolocations",
            "new_country", "credential_attack_volume", "spray_infrastructure",
            "azure_risk", "rare_country", "failed_signin_burst", "legacy_auth",
        ]
        order = list(catalog.CATALOG)
        got = [k for k in order if k in set(before)]
        self.assertEqual(got, before,
                         "a catalog insertion reordered the pre-existing kinds")


class TestNormalize(unittest.TestCase):
    def test_seconds_are_converted_to_millis(self):
        a = anomaly_({"incident_id": "i", "severity": "info",
                               "event_time_epoch": 1788169816, "reasons": []})
        self.assertEqual(a["ts"], 1788169816000)

    def test_spray_finding_survives_without_an_entity(self):
        """No user_email, only _timestamp_ms. It belongs in the feed regardless."""
        a = anomaly_({
            "incident_id": "s1", "severity": "error",
            "detection_kind": "azure_ad_spray_infrastructure",
            "_timestamp_ms": 1788169816000, "ip": "9.9.9.9",
            "reasons": [{"kind": "spray_infrastructure", "why": "one IP, forty accounts"}],
        })
        self.assertIsNone(a["entity"])
        self.assertIsNone(a["entity_type"])
        self.assertEqual(a["ts"], 1788169816000)
        self.assertEqual(a["detection_id"], "spray_infrastructure")

    def test_telemetry_is_not_a_finding(self):
        for kind in catalog.TELEMETRY_KINDS:
            self.assertIsNone(anomaly_({"detection_kind": kind, "incident_id": "x"}))

    def test_the_wire_name_is_the_raw_reason_kind(self):
        """The console names a detection exactly as the logs do.

        `name` is what every page prints; if a curated `Detection.name` ever
        reaches it again, an analyst reading the console has no string to search
        Coralogix for.
        """
        a = anomaly_({
            "incident_id": "i1", "severity": "warning", "_timestamp_ms": 1,
            "detection_kind": "azure_ad_credential_abuse_deviation",
            "reasons": [{"kind": "new_app", "why": "never opened before"}],
        })
        self.assertEqual(a["name"], "new_app")
        self.assertEqual(a["name"], a["detection_id"])
        # The record-level kind rides along verbatim, but is not the identity.
        self.assertEqual(a["detection_kind"], "azure_ad_credential_abuse_deviation")
        self.assertEqual(a["evidence"]["source"], "new_app")

    def test_no_curated_catalog_name_reaches_the_wire(self):
        """Every kind in the catalog, checked against its own prose."""
        prose = {d.name for d in catalog.CATALOG.values()} | {catalog.UNKNOWN.name}
        for kind in catalog.CATALOG:
            a = anomaly_({
                "incident_id": f"i-{kind}", "severity": "warning",
                "_timestamp_ms": 1, "reasons": [{"kind": kind, "why": "w"}],
            })
            self.assertEqual(a["name"], kind)
            self.assertNotIn(a["name"], prose, f"{kind} still renders as prose")
            self.assertNotIn(a["evidence"]["source"], prose)

    def test_mitre_still_comes_from_the_catalog(self):
        """The relabel was display-only: MITRE and weight must be untouched."""
        a = anomaly_({
            "incident_id": "i", "severity": "critical", "_timestamp_ms": 1,
            "reasons": [{"kind": "impossible_travel", "why": "w"}],
        })
        det = catalog.get("impossible_travel")
        self.assertEqual(a["mitre_tactic"], det.tactic)
        self.assertEqual(a["mitre_technique"], det.technique)
        self.assertEqual(a["weight"], catalog.weight_for("impossible_travel", "critical"))

    def test_observed_falls_back_to_no_prose(self):
        """A reasonless finding shows nothing rather than an invented sentence."""
        a = anomaly_({
            "incident_id": "i", "severity": "info", "_timestamp_ms": 1,
            "detection_kind": "azure_ad_credential_abuse_deviation",
        })
        self.assertEqual(a["observed"], "")

    def test_observed_prefers_the_primary_reasons_sentence(self):
        a = anomaly_({
            "incident_id": "i", "severity": "warning",
            "_timestamp_ms": 1, "reasons": [
                {"kind": "unusual_hour", "why": "cheap check"},
                {"kind": "impossible_travel", "why": "the real reason"},
            ]})
        self.assertEqual(a["observed"], "the real reason")


class TestLiveStream(unittest.TestCase):
    """Checked against S3 when credentials are available, skipped otherwise."""

    def test_every_observed_kind_is_known(self):
        if not os.environ.get("UEBA_LIVE_TESTS"):
            self.skipTest("set UEBA_LIVE_TESTS=1 (and an AWS profile) to run")
        import collections

        from serving import s3store

        keys = [k for k in s3store.list_keys(T.bucket, T.anomaly_prefix + "/")
                if k.endswith(".json")]
        self.assertTrue(keys, "no anomaly objects in S3 — run backfill.py first")
        unknown: collections.Counter = collections.Counter()
        for blob in s3store.get_many_json(T.bucket, keys):
            for rec in blob or []:
                if rec.get("detection_kind") in catalog.TELEMETRY_KINDS:
                    continue
                for r in rec.get("reasons") or []:
                    k = r.get("kind") if isinstance(r, dict) else None
                    if k and k not in catalog.CATALOG:
                        unknown[k] += 1
        self.assertEqual({}, dict(unknown),
                         "reason kinds in the live stream are missing from CATALOG")


if __name__ == "__main__":
    unittest.main(verbosity=2)
