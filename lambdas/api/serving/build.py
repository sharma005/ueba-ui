"""The snapshot builder: 150 MB of baseline + 30 days of findings -> small JSON.

Run every two hours, right after the detector refreshes the baseline. It exists
because `baseline.json` is 150 MB and the 30-day finding set is another 90 MB:
neither can be touched on a request path that has to answer in milliseconds. So
all the expensive work — parsing, joining, scoring, chaining — happens once here,
and the API only ever reads small precomputed objects.

`meta.json` is written **last**. Until it lands, a half-finished snapshot is not
advertised as current, so a build that dies midway leaves the previous snapshot
serving rather than a torn one.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import logging
import os
import time

from . import catalog, config, derive, links, normalize, s3store
from .tenants import Tenant

log = logging.getLogger("build")

DAY_MS = derive.DAY_MS
NOTABLE_LIMIT = 50
# An entity page shows a finding list, not an archive, so it is capped. Measured
# on real data: across 7,296 accounts the noisiest carries 68 findings in 30
# days and exactly one exceeds 50 — a cap of 50 silently truncated that account
# to 50, so its page and its Score Contributions disagreed with the count on the
# Users roster. 250 leaves the cap well clear of the data while still bounding a
# shard, and `anomalies_truncated` below makes any future truncation visible
# rather than silent.
ENTITY_ANOMALY_CAP = 250
SPARKLINE_DAYS = 14


# --- Baseline -------------------------------------------------------------


def _counts(m: object) -> dict:
    """A name->count map from the baseline, or an empty one if it is absent."""
    return m if isinstance(m, dict) and m else {}


def _hist(m: object, slots: int) -> list[int]:
    """A `{"3": 12}` histogram from the baseline as a dense list."""
    h = _counts(m)
    return [int(h.get(str(i), 0) or 0) for i in range(slots)]


def compact_profile(u: dict, lookback_days: int) -> dict:
    """One baseline user record -> the `Profile` shape `adapt.ts` reads.

    Every counter the detector wrote is carried through untruncated: the
    Learned Baseline card renders the record as it is, so a top-N here would
    silently decide which of an account's addresses or apps the analyst is
    allowed to see. `ENTITY_SHARDS` is sized for the resulting document — see
    the note on it in `config.py`.

    Only the shape changes: histograms become dense lists, epoch seconds become
    millis, and the record's own key becomes `identifiers`.
    """
    return {
        # The UI renders this as "{days_seen}-day baseline", i.e. the window the
        # profile covers — the baseline builder's own `lookback_days`, not a
        # count of active days (which the record does not carry).
        "days_seen": lookback_days,
        "first_seen_ms": int((u.get("first_seen") or 0) * 1000),
        "last_seen_ms": int((u.get("last_seen") or 0) * 1000),
        "login_count": int(u.get("login_count") or 0),
        "failed_login_count": int(u.get("failed_login_count") or 0),
        "distinct_ips": len(_counts(u.get("ips"))),
        "distinct_failed_ips": int(u.get("distinct_failed_ips") or 0),
        # Addresses the account signed in from. `/entities/:id/graph` reads this
        # server-side too, taking its own top 8.
        "ips": _counts(u.get("ips")),
        # Per-address country/city/ASN from the Coralogix enrichment. The Source
        # IPs panel annotates each row from it.
        "ip_geo": _counts(u.get("ip_geo")),
        "countries": _counts(u.get("countries")),
        "cities": _counts(u.get("cities")),
        "asns": _counts(u.get("asns")),
        "devices": _counts(u.get("devices")),
        "apps": _counts(u.get("apps")),
        "client_apps": _counts(u.get("client_apps")),
        "auth_requirements": _counts(u.get("auth_requirements")),
        # Failure counters are kept apart from the success-only profile above,
        # exactly as the detector writes them: a password-spray burst must not
        # widen what this account counts as normal.
        "failed_ips": _counts(u.get("failed_ips")),
        "failed_countries": _counts(u.get("failed_countries")),
        # Azure AD sign-ins name no host, so this stays empty and the UI renders
        # its empty state rather than a fabricated list.
        "hosts": {},
        # IST, per the baseline's own `tz_hour_histogram`.
        "hours_hist": _hist(u.get("hour_histogram"), 24),
        # Index 0 is Monday (Python's `weekday()`). Verified against the live
        # baseline: normalising for how often each weekday falls in the window,
        # 5 and 6 sit ~20% below the rest, which is the weekend.
        "dow_hist": _hist(u.get("dow_histogram"), 7),
        # The baseline keys users by resolved address, so the address *is* the
        # identifier. One entry, weighted by how often it was seen, which is
        # what `displayAddress` and the merged-identities panel expect.
        "identifiers": {u.get("user_email"): int(u.get("login_count") or 0)}
        if u.get("user_email") else {},
        "user_name": u.get("user_name"),
        # No behavioural clusterer runs in this pipeline, so there is no peer
        # cohort. Explicit nulls: the UI has an honest empty state for both.
        "peer_group": None,
        "peer_stats": None,
    }


def load_baseline(t: Tenant, local_dir: str = "/tmp/ueba-ui") -> tuple[dict, dict]:
    """Pull and parse the baseline. Returns (profiles_by_entity, baseline_meta)."""
    path = os.path.join(local_dir, "baseline.json")
    t0 = time.time()
    s3store.download(t.bucket, t.baseline_key, path)
    size = os.path.getsize(path)
    log.info("baseline downloaded: %.1f MB in %.1fs", size / 1e6, time.time() - t0)

    t0 = time.time()
    with open(path) as f:
        doc = json.load(f)
    log.info("baseline parsed in %.1fs", time.time() - t0)

    lookback = int(doc.get("lookback_days") or 30)
    users = doc.get("users") or {}
    profiles = {
        str(email).strip().lower(): compact_profile(u, lookback)
        for email, u in users.items()
        if email
    }
    # Whether the detector finished learning this tenant, from its own record of
    # the build. Distinct from the `baseline_status` telemetry, which reports on
    # the *last run* and can say "ok" while the baseline it produced is still
    # only part of the history — exactly deel's state today. An account the
    # detector has not seen yet has its findings suppressed entirely, so an
    # incomplete baseline shows up as a quiet console rather than as an error,
    # and the console has to be able to say which of the two it is looking at.
    incremental = doc.get("incremental") or {}
    meta = {
        "generated_at": doc.get("generated_at"),
        "users_with_baseline": int(doc.get("users_with_baseline") or len(profiles)),
        "total_login_events_seen": int(doc.get("total_login_events_seen") or 0),
        "lookback_days": lookback,
        "source": "/".join(str(doc.get(k) or "?") for k in
                           ("source_application", "source_subsystem", "source_category")),
        "window_start_epoch": doc.get("window_start_epoch"),
        "window_end_epoch": doc.get("window_end_epoch"),
        "incomplete": bool(incremental.get("full_build_incomplete")),
        "data_miss_risk": bool(incremental.get("data_miss_risk")),
        # How far back the detector has actually filled in, when it is still
        # working backwards. Null once the build is complete.
        "resume_before": incremental.get("resume_before_iso"),
    }
    # The parsed baseline is the biggest thing in memory; let it go now.
    del doc, users
    try:
        os.remove(path)
    except OSError:
        pass
    return profiles, meta


# --- Findings -------------------------------------------------------------


def load_findings(t: Tenant, now_ms: int,
                  days: int = derive.WINDOW_DAYS) -> tuple[list[dict], list[dict]]:
    """Read the day-partitioned anomaly store. Returns (anomalies, telemetry).

    Anomalies are normalized here and deduped by `anomaly_id`: the backfill and
    the patched detector can both have written a day (and the detector's own
    `incident_id` is stable across its 2-hourly runs), so overlap is expected
    rather than exceptional.
    """
    floor_ms = now_ms - days * 86_400_000
    floor_day = (dt.datetime.fromtimestamp(floor_ms / 1000, dt.timezone.utc)
                 .strftime("%Y-%m-%d"))
    keys = [
        k for k in s3store.list_keys(t.bucket, t.anomaly_prefix + "/")
        if "/dt=" in k and k.split("/dt=")[1].split("/")[0] >= floor_day
    ]
    log.info("reading %d anomaly object(s) from %s onwards", len(keys), floor_day)
    lk = links.for_tenant(t)
    blobs = s3store.get_many_json(t.bucket, keys)

    seen: set[str] = set()
    anomalies: list[dict] = []
    telemetry: list[dict] = []
    raw_count = 0
    for blob in blobs:
        if not blob:
            continue
        records = blob if isinstance(blob, list) else blob.get("anomalies") or []
        for rec in records:
            if not isinstance(rec, dict):
                continue
            raw_count += 1
            if rec.get("detection_kind") in catalog.TELEMETRY_KINDS:
                telemetry.append(rec)
                continue
            a = normalize.anomaly(rec, lk)
            if not a or not a["ts"]:
                continue
            # The store is partitioned by day, so the oldest key pulled in
            # holds up to a full extra day of findings either side of the
            # cutoff. Without this the window is 30-31 days wide depending on
            # the hour of the run, and `n_anomalies` came out one or two above
            # what any `now - 30d` view of the same data counts.
            if a["ts"] < floor_ms:
                continue
            if a["anomaly_id"] in seen:
                continue
            seen.add(a["anomaly_id"])
            anomalies.append(a)

    anomalies.sort(key=lambda a: -a["ts"])
    log.info("normalized %d finding(s) from %d record(s); %d telemetry",
             len(anomalies), raw_count, len(telemetry))
    return anomalies, telemetry


# --- Assembly -------------------------------------------------------------


def _overview(anomalies: list[dict], rows: dict[str, dict],
              baseline_meta: dict, now_ms: int) -> dict:
    today = derive._day_str(now_ms)
    per_day: collections.Counter = collections.Counter()
    for a in anomalies:
        per_day[derive._day_str(a["ts"])] += 1

    trend = []
    for i in range(derive.WINDOW_DAYS - 1, -1, -1):
        d = derive._day_str(now_ms - i * DAY_MS)
        trend.append({"dt": d, "anomalies": per_day.get(d, 0)})

    kinds: collections.Counter = collections.Counter(a["detection_id"] for a in anomalies)
    mitre: collections.Counter = collections.Counter(
        (a["mitre_tactic"], a["mitre_technique"]) for a in anomalies
    )
    bands: collections.Counter = collections.Counter(r["band"] for r in rows.values())

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "day": today,
        "kpis": {
            # Everyone the detector has a behavioural profile for, not just
            # those with a finding — that is what "monitored" means.
            "users_monitored": baseline_meta.get("users_with_baseline", 0),
            # Azure AD sign-ins carry no host entity. Zero, not omitted, so the
            # UI shows a real zero instead of a blank tile.
            "hosts_monitored": 0,
            "notable_entities": bands.get("notable", 0),
            "high_entities": bands.get("high", 0),
            "anomalies_today": per_day.get(today, 0),
            "max_score": max((r["score"] for r in rows.values()), default=0),
        },
        "trend": trend,
        "top_detections": [
            {"detection_id": k, "name": catalog.label(k), "count": n}
            for k, n in kinds.most_common(8)
        ],
        "mitre": [
            {"mitre_tactic": t, "mitre_technique": q, "count": n}
            for (t, q), n in mitre.most_common(12)
        ],
        "bands": {b: bands.get(b, 0) for b in ("notable", "high", "medium", "low")},
    }


def _pipeline_health(telemetry: list[dict]) -> dict:
    """What the detector says about itself, from its own run-summary records."""
    runs = [r for r in telemetry
            if r.get("detection_kind") in catalog.RUN_SUMMARY_KINDS]
    runs.sort(key=lambda r: str(r.get("run_at_iso") or ""), reverse=True)
    health = [r for r in telemetry
              if r.get("detection_kind") in catalog.BASELINE_HEALTH_KINDS]
    health.sort(key=lambda r: str(r.get("ts") or r.get("run_at_iso") or ""), reverse=True)
    last = runs[0] if runs else {}
    return {
        "last_run_at": last.get("run_at_iso"),
        "last_run_events_scored": last.get("events_scored"),
        "last_run_anomalies_emitted": last.get("anomalies_emitted"),
        "runs_seen": len(runs),
        "baseline_status": (health[0].get("status") if health else None),
        "detector_id": last.get("detector_id"),
    }


def build(t: Tenant, now_ms: int | None = None) -> dict:
    now_ms = now_ms or int(time.time() * 1000)
    t_start = time.time()

    profiles, baseline_meta = load_baseline(t)
    anomalies, telemetry = load_findings(t, now_ms)

    by_entity: dict[str, list[dict]] = collections.defaultdict(list)
    for a in anomalies:
        if a.get("entity"):
            by_entity[a["entity"]].append(a)

    rows = derive.entity_rows(by_entity, now_ms)
    # Everyone with a baseline belongs in the index even with no findings: a
    # quiet account scoring 0 is a real answer, and the Users page has to be
    # able to find it.
    for entity in profiles:
        if entity not in rows:
            rows[entity] = {
                "entity": entity, "entity_type": "user", "score": 0, "band": "low",
                "delta_24h": 0, "last_anomaly_ts": 0, "n_anomalies_24h": 0,
                "n_anomalies": 0,
            }

    chains = derive.chains(by_entity, now_ms)
    overview = _overview(anomalies, rows, baseline_meta, now_ms)

    writes: list[tuple[str, object]] = []
    P = t.snapshot_prefix

    writes.append((f"{P}/overview.json", overview))

    ranked = sorted(rows.values(), key=lambda r: (-r["score"], -r["last_anomaly_ts"]))
    # `delta_24h` and the two counts are carried here, not just in notables.
    # The Users page lists the whole population from this index; when the counts
    # lived only in `notables.json` (top 50) every other account rendered a
    # hard-coded 0 anomalies however many it actually had.
    writes.append((f"{P}/entities.json", {
        "entities": [
            {"entity": r["entity"], "entity_type": "user", "score": r["score"],
             "band": r["band"], "last_anomaly_ts": r["last_anomaly_ts"],
             "delta_24h": r["delta_24h"],
             "n_anomalies": r["n_anomalies"],
             "n_anomalies_24h": r["n_anomalies_24h"]}
            for r in ranked
        ],
        "total": len(ranked),
    }))

    notables = []
    for r in ranked[:NOTABLE_LIMIT]:
        anoms = by_entity.get(r["entity"], [])
        notables.append({
            "entity": r["entity"], "entity_type": "user", "score": r["score"],
            "band": r["band"], "delta_24h": r["delta_24h"],
            "n_anomalies_24h": r["n_anomalies_24h"],
            "top_detections": derive.top_detections(anoms),
            "sparkline": derive.history(anoms, now_ms, SPARKLINE_DAYS),
        })
    writes.append((f"{P}/notables.json", {"entities": notables}))

    per_day: collections.Counter = collections.Counter()
    for a in anomalies:
        per_day[derive._day_str(a["ts"])] += 1
    writes.append((f"{P}/feed.json", {
        "anomalies": anomalies[:config.FEED_SIZE],
        "total": len(anomalies),
        "per_day": dict(per_day),
        "truncated": len(anomalies) > config.FEED_SIZE,
    }))

    writes.append((f"{P}/chains.json", {
        "generated_at": overview["generated_at"],
        "day": overview["day"],
        "chains": chains,
        "tactic_ranks": catalog.TACTIC_RANKS,
        "chain_alert_score": derive.CHAIN_ALERT_SCORE,
    }))

    # Entity pages, sharded. Every entity with a profile or a finding gets one.
    shards: dict[str, dict] = collections.defaultdict(dict)
    for entity, r in rows.items():
        anoms = sorted(by_entity.get(entity, []), key=lambda a: -a["ts"])[:ENTITY_ANOMALY_CAP]
        shards[config.entity_shard(entity)][entity] = {
            "entity": entity,
            "entity_type": "user",
            "score": r["score"],
            "band": r["band"],
            "delta_24h": r["delta_24h"],
            # True totals, independent of the capped list below, so a page can
            # never quietly disagree with the roster about how many findings an
            # account has.
            "n_anomalies": r["n_anomalies"],
            "n_anomalies_24h": r["n_anomalies_24h"],
            "anomalies_truncated": r["n_anomalies"] > ENTITY_ANOMALY_CAP,
            "top_detections": derive.top_detections(anoms),
            "profile": profiles.get(entity, {}),
            "score_history": derive.history(anoms, now_ms, derive.WINDOW_DAYS),
            "anomalies": anoms,
            # No `timeline` here on purpose: every field of it is derivable from
            # `anomalies` by `normalize.timeline_item`, and storing both doubled
            # the shard for nothing. The API builds it per request.
        }
    # Gzipped: the feed and the shards are the only objects big enough for it to
    # matter, and JSON of this shape compresses about 8x. `get_json` sniffs the
    # magic bytes, so a reader needs no special case.
    fat: list[tuple[str, object]] = [
        (f"{P}/entity/shard-{shard}.json.gz", docs) for shard, docs in shards.items()
    ]
    feed = [w for w in writes if w[0].endswith("/feed.json")]
    writes = [w for w in writes if not w[0].endswith("/feed.json")]
    fat.extend((k.replace("/feed.json", "/feed.json.gz"), v) for k, v in feed)

    written = s3store.put_many_json(t.bucket, writes)
    written += s3store.put_many_json(t.bucket, fat, gzip_it=True)

    meta = {
        "tenant": t.id,
        "generated_at": overview["generated_at"],
        "generated_at_ms": now_ms,
        "build_seconds": round(time.time() - t_start, 2),
        "entities": len(rows),
        "entities_with_findings": len(by_entity),
        "anomalies": len(anomalies),
        "feed_size": min(len(anomalies), config.FEED_SIZE),
        "chains": len(chains),
        "shards": len(shards),
        "bytes_written": written,
        "window_days": derive.WINDOW_DAYS,
        "half_life_days": derive.HALF_LIFE_DAYS,
        "bands": derive.BANDS,
        "detections_enabled": catalog.enabled_count(),
        "baseline": baseline_meta,
        "pipeline": _pipeline_health(telemetry),
    }
    # Last, deliberately: see the module docstring.
    s3store.put_json(t.bucket, f"{P}/meta.json", meta)
    log.info("snapshot built for %s in %.1fs: %s", t.id, meta["build_seconds"],
             {k: meta[k] for k in ("entities", "anomalies", "chains", "shards")})
    return meta
