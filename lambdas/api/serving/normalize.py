"""Detector records -> the wire shapes in `ui/src/types.ts`.

One-way and lossy on purpose: the detector's record carries ~40 fields, of which
the UI reads a dozen directly and shows the rest as evidence. Everything the
adapters in `ui/src/data/adapt.ts` touch is produced here.

Two record shapes arrive on the same stream:

  * per-account findings — `user_email` set, `event_time_epoch` in **seconds**;
  * `azure_ad_spray_infrastructure` — about an address, not a person, so
    `user_email` is null and only `_timestamp_ms` carries the time.

`Anomaly.entity` is optional in types.ts and `fromAnomaly` renders a missing one
as an em-dash, so a spray finding stays in the feed and in the MITRE counts
while attaching to no account. That is the honest handling: inventing a
pseudo-entity for an attacker IP would put it in the Users list.
"""

from __future__ import annotations

from . import catalog, links

# Everything the UI shows as evidence, in the order an analyst reads it. Absent
# and null fields are dropped rather than rendered as "None".
_EVIDENCE_FIELDS = (
    "ip", "country", "city", "asn", "asn_category", "device_fingerprint",
    "device_trust", "user_agent", "app_name", "client_app", "risk_level",
    "risk_state", "conditional_access_status", "authentication_requirement",
    "error_code", "is_failure", "session_id", "confidence",
    "ml_anomaly_score", "ml_profile_confidence", "ml_score_components",
    "distinct_users_targeted", "failed_attempts", "window_hours",
    "sample_event_ids", "event_id", "account_type",
)


def ts_ms(record: dict) -> int:
    """Event time in epoch **milliseconds** — what every UI helper expects.

    `_timestamp_ms` is what the detector hands the ingest client and is set on
    both record shapes; `event_time_epoch` is seconds and per-account only.
    Preferring the former keeps spray findings, which have no epoch field,
    on the same timeline.
    """
    ms = record.get("_timestamp_ms")
    if isinstance(ms, (int, float)) and ms > 0:
        return int(ms)
    secs = record.get("event_time_epoch")
    if isinstance(secs, (int, float)) and secs > 0:
        return int(secs * 1000)
    return 0


def _baseline_phrase(record: dict) -> str:
    """A short "what normal looks like" line from `baseline_summary`.

    The UI prints this straight after the observation as
    "<observed> (baseline <this>)", so it has to read as a clause, not a dump.
    """
    bs = record.get("baseline_summary")
    if not isinstance(bs, dict):
        return ""
    bits: list[str] = []
    logins = bs.get("baseline_login_count")
    if isinstance(logins, int) and logins > 0:
        bits.append(f"{logins} sign-ins profiled")
    countries = bs.get("top_countries")
    if isinstance(countries, dict) and countries:
        top = sorted(countries.items(), key=lambda kv: -kv[1])[:2]
        bits.append("usually " + ", ".join(name for name, _ in top))
    ips = bs.get("distinct_ips")
    if isinstance(ips, int) and ips > 0:
        bits.append(f"{ips} known addresses")
    failed = bs.get("baseline_failed_login_count")
    if isinstance(failed, int) and failed > 0:
        bits.append(f"{failed} prior failures")
    return "; ".join(bits)


def _observed(record: dict, primary: str) -> str:
    """The detector's own sentence for the primary reason.

    `why` is written for a human and is the single most useful field on the
    record, so it is passed through verbatim rather than re-summarised.
    """
    for r in record.get("reasons") or []:
        if isinstance(r, dict) and r.get("kind") == primary and r.get("why"):
            return str(r["why"])
    for r in record.get("reasons") or []:
        if isinstance(r, dict) and r.get("why"):
            return str(r["why"])
    # No curated blurb fallback: the console shows only what the logs said. Every
    # finding in the 30-day store carries a `why`, so this is defensive, and an
    # empty string renders as an absent row rather than as invented prose.
    return ""


def anomaly(record: dict, lk: links.LinkConfig) -> dict | None:
    """One detector record -> one `Anomaly`. None for telemetry records."""
    if record.get("detection_kind") in catalog.TELEMETRY_KINDS:
        return None
    ident = record.get("incident_id") or record.get("event_id")
    if not ident:
        return None

    primary = catalog.primary_kind(record)
    det = catalog.get(primary)
    severity = record.get("severity")
    entity = (record.get("user_email") or "").strip().lower() or None

    evidence: dict = {}
    for f in _EVIDENCE_FIELDS:
        v = record.get(f)
        if v is not None and v != "" and v != {}:
            evidence[f] = v
    # Every reason, not just the primary — the "why this scored" panel opens on
    # the full set, and a finding's second reason is often what makes it real.
    reasons = [r for r in (record.get("reasons") or []) if isinstance(r, dict)]
    if reasons:
        evidence["reasons"] = reasons
    evidence["severity"] = severity
    evidence["detection_kind"] = record.get("detection_kind")
    # `fromAnomaly` prefers `evidence.source` for the "source" column.
    evidence["source"] = catalog.label(primary)

    ts = ts_ms(record)
    incident_id = record.get("incident_id") or str(ident)
    sample_ids = record.get("sample_event_ids") or (
        [record["event_id"]] if record.get("event_id") else []
    )

    return {
        "anomaly_id": str(ident),
        # The detector's own id, named as it is in the log. `anomaly_id` carries
        # the same value, but an analyst pivoting to Coralogix searches for
        # `incident_id`, so the field it is called there is the field to show.
        "incident_id": str(incident_id),
        "event_ids": [str(e) for e in sample_ids if e],
        # None unless CORALOGIX_UI_BASE is configured — see links.py.
        "log_url": lk.finding_url(incident_id, ts),
        "signin_url": lk.signin_url(sample_ids, ts),
        "ts": ts,
        "detection_id": primary,
        # The raw reason kind, twice: `detection_id` is the identity the API
        # filters and scores on, `name` is what the console prints. They are the
        # same string by design — see `catalog.label`.
        "name": catalog.label(primary),
        # The record-level kind, verbatim. Far coarser than `detection_id`
        # (~97% of findings are `azure_ad_credential_abuse_deviation`), so it is
        # carried for the detail view and for pivoting into Coralogix, never as
        # the grouping key.
        "detection_kind": record.get("detection_kind"),
        "entity": entity,
        "entity_type": "user" if entity else None,
        "weight": catalog.weight_for(primary, severity),
        "observed": _observed(record, primary),
        "baseline": _baseline_phrase(record),
        "mitre_tactic": det.tactic,
        "mitre_technique": det.technique,
        "evidence": evidence,
    }


def timeline_item(a: dict, fallback_source: str) -> dict:
    """An `Anomaly` -> a `TimelineItem` for the entity page.

    Raw sign-ins are not in S3 (only the detector's findings are), so an
    entity's timeline is built from its anomalies. `kind: "anomaly"` is
    therefore the only kind emitted, and `bucketTimeline` groups these by hour.

    `fallback_source` is the tenant's own `source_label` — what to call the log
    stream when a finding names no application. This runs on the request path,
    where the tenant is known, so it is passed rather than looked up.
    """
    ev = a.get("evidence") or {}
    return {
        "kind": "anomaly",
        "ts": a["ts"],
        "anomaly_id": a["anomaly_id"],
        "name": a["name"],
        "detection_id": a["detection_id"],
        "weight": a["weight"],
        "observed": a["observed"],
        "baseline": a["baseline"],
        "mitre_tactic": a["mitre_tactic"],
        "mitre_technique": a["mitre_technique"],
        "src_ip": ev.get("ip"),
        "geo_city": ev.get("city"),
        "geo_country": ev.get("country"),
        "app": ev.get("app_name"),
        # The application signed into, or the feed itself when a finding names no
        # app (spray findings are about an address, not a session). NOT the
        # detection name: the timeline already renders that as the row title and
        # as `ruleName`, and a third copy in the "source" slot pushed the real
        # context — address, location, app — out of view.
        "source": ev.get("app_name") or fallback_source,
        "outcome": "failure" if ev.get("is_failure") else "success",
        "event_type": a["detection_id"],
    }
