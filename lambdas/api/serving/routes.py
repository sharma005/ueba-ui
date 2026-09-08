"""`/api/*` request handling, independent of how the request arrived.

Framework-free on purpose: the same `handle()` serves the local dev server and
the Lambda Function URL, so what you exercise with vite in front of it is
byte-for-byte what runs deployed.

Every read is a small GET against the snapshot the builder wrote, cached in the
module by ETag. Nothing here parses the baseline or talks to Coralogix, so a
cold response is a couple of object reads and a warm one is memory.

The surface is entirely read-only. There is no mutation route, so nothing the
console does can alter pipeline state.

Every handler takes the `Tenant` it is answering for as its first argument,
resolved once in `handle()` from `?tenant=`. Nothing reads a tenant from module
state, so a handler cannot accidentally serve the wrong customer — it would
have to be handed the wrong one.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from urllib.parse import unquote

from . import catalog, config, derive, links, normalize, s3store, tenants
from .tenants import Tenant

log = logging.getLogger("routes")

DAY_MS = derive.DAY_MS

# Node budget for an entity's relationship graph. A force-directed layout stops
# being readable well before this, and the profile's top-10 maps are already
# ranked, so the cut takes the least-seen items.
GRAPH_MAX_NODES = 28


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# --- helpers ---------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def _int(query: dict, name: str, default: int, lo: int, hi: int) -> int:
    raw = query.get(name)
    if raw is None or raw == "":
        return default
    try:
        return max(lo, min(hi, int(float(raw))))
    except (TypeError, ValueError):
        return default


def _feed(t: Tenant) -> dict:
    return s3store.get_json_cached(t.bucket, t.feed_key, default={}) or {}


def _snapshot(t: Tenant, name: str, default):
    return s3store.get_json_cached(t.bucket, t.snapshot_key(name), default=default)


def _snapshot_shards(t: Tenant) -> int | None:
    """How many shards the snapshot in hand was written with.

    From `meta.json`, not from `config.ENTITY_SHARDS`: the builder and this API
    are separate deployments, so the count compiled in here is only what the
    *next* build will use. Reading the snapshot's own number means an entity
    page can never be resolved to an object the current build does not write.
    """
    meta = _snapshot(t, "meta.json", default={}) or {}
    n = meta.get("shards")
    return n if isinstance(n, int) and n > 0 else None


def _entity_doc(t: Tenant, entity: str) -> dict | None:
    """One entity's page document, from its shard."""
    key = entity.strip().lower()
    shard_key = t.entity_shard_key(key, _snapshot_shards(t))
    shard = s3store.get_json_cached(t.bucket, shard_key, default={}) or {}
    return shard.get(key)


# --- handlers --------------------------------------------------------------


def health(t: Tenant, _query: dict) -> dict:
    meta = _snapshot(t, "meta.json", default={}) or {}
    baseline = meta.get("baseline") or {}
    pipeline = meta.get("pipeline") or {}
    built_ms = meta.get("generated_at_ms") or 0
    age_s = int((_now_ms() - built_ms) / 1000) if built_ms else None
    # The detector runs every 2h and the builder follows it, so a snapshot older
    # than three cycles means something upstream stopped.
    fresh = age_s is not None and age_s < 3 * 2 * 3600
    return {
        "ok": bool(meta) and fresh and pipeline.get("baseline_status") in (None, "ok"),
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        # What the detector actually reads, from the baseline it wrote.
        "sources": [baseline.get("source")] if baseline.get("source") else [],
        "detections_enabled": meta.get("detections_enabled", catalog.enabled_count()),
        # Which customer answered. Named explicitly so a request that omitted
        # `?tenant=` and fell through to the default says so, rather than
        # leaving the caller to assume it got what it meant.
        "tenant": t.id,
        "tenant_label": t.label,
        "api_key_required": bool(config.API_KEY),
        # False means CORALOGIX_UI_BASE is unset, so findings carry no log link.
        "log_links_configured": links.for_tenant(t).configured(),
        # Beyond the UI's Health interface, for ops: everything needed to tell
        # a stalled pipeline from a stalled builder.
        "snapshot_generated_at": meta.get("generated_at"),
        # The exact `now_ms` the build counted from, not `generated_at` (which
        # is a second clock read, taken after the build). Any client slicing a
        # window must anchor to this: counting `Date.now() - 30d` in a browser
        # against a snapshot built up to 2h ago drops the findings in between,
        # which is how a roster card and an entity page came to print 27 and 26
        # for the same account.
        "snapshot_generated_at_ms": built_ms or None,
        "snapshot_age_seconds": age_s,
        "entities": meta.get("entities"),
        "anomalies_30d": meta.get("anomalies"),
        "baseline_generated_at": baseline.get("generated_at"),
        "users_with_baseline": baseline.get("users_with_baseline"),
        # True while the detector is still working backwards through this
        # tenant's history. Findings are suppressed for accounts it has not seen
        # yet, so a half-learned baseline reads as a quiet customer unless the
        # console says otherwise — see the notice in `Shell`.
        "baseline_incomplete": baseline.get("incomplete"),
        "baseline_resume_before": baseline.get("resume_before"),
        "pipeline": pipeline,
        # Named because a builder/API disagreement here is invisible from every
        # other field: the feed stays current while entity pages resolve to
        # shards nothing rewrites. `_entity_doc` follows the snapshot's count,
        # so this is a redeploy reminder rather than an outage.
        "entity_shards": meta.get("shards"),
        "entity_shards_expected": config.ENTITY_SHARDS,
    }


def overview(t: Tenant, _query: dict) -> dict:
    return _snapshot(t, "overview.json", default={}) or {}


def notables(t: Tenant, _query: dict) -> dict:
    return _snapshot(t, "notables.json", default={"entities": []}) or {"entities": []}


def entities(t: Tenant, query: dict) -> dict:
    doc = _snapshot(t, "entities.json", default={"entities": [], "total": 0}) or {}
    rows = doc.get("entities") or []

    q = (query.get("q") or "").strip().lower()
    band = (query.get("band") or "").strip().lower()
    etype = (query.get("type") or "").strip().lower()

    if q:
        rows = [r for r in rows if q in r["entity"].lower()]
    if band:
        rows = [r for r in rows if r.get("band") == band]
    if etype:
        # Neither Azure AD nor Okta sign-ins name a host entity, so `type=host`
        # correctly returns nothing and the Dashboard's host panel shows its
        # empty state.
        rows = [r for r in rows if r.get("entity_type") == etype]

    limit = _int(query, "limit", 500, 1, 5000)
    return {"entities": rows[:limit], "total": len(rows)}


def entity(t: Tenant, entity_id: str, _query: dict) -> dict:
    doc = _entity_doc(t, entity_id)
    if doc is None:
        raise HttpError(404, f"no entity {entity_id!r} in the current snapshot")
    anoms = doc.get("anomalies") or []
    return {
        **doc,
        "anomalies": anoms,
        # Derived here rather than stored: see build.py's shard comment.
        "timeline": [normalize.timeline_item(a, t.source_label) for a in anoms],
    }


def entity_graph(t: Tenant, entity_id: str, _query: dict) -> dict:
    """The entity's relationships, from its baseline profile.

    Built from the profile's counters, not from a graph store: what this
    pipeline knows about an account is which addresses, networks, countries and
    apps it used, and how often. Edge weight is that observation count, so the
    thick edges are the account's habitual context and the thin ones are the
    outliers worth reading.
    """
    doc = _entity_doc(t, entity_id)
    if doc is None:
        raise HttpError(404, f"no entity {entity_id!r} in the current snapshot")
    profile = doc.get("profile") or {}
    key = entity_id.strip().lower()

    nodes = [{"id": key, "type": "user", "score": doc.get("score", 0)}]
    links = []
    # `ip` and `app` are colours the graph already knows; `asn` and `country`
    # fall through to its neutral tone, which is the right emphasis for them.
    groups = (
        ("ip", profile.get("ips") or {}, 8),
        ("app", profile.get("apps") or {}, 8),
        ("asn", profile.get("asns") or {}, 5),
        ("country", profile.get("countries") or {}, 4),
    )
    for kind, counts, cap in groups:
        for name, n in sorted(counts.items(), key=lambda kv: -(kv[1] or 0))[:cap]:
            if not name or len(nodes) >= GRAPH_MAX_NODES:
                break
            nid = f"{kind}:{name}"
            nodes.append({"id": nid, "type": kind, "score": 0})
            links.append({"source": key, "target": nid, "weight": int(n or 0)})
    return {"entity": key, "nodes": nodes, "links": links}


def entity_events(t: Tenant, entity_id: str, query: dict) -> dict:
    """The entity's recent activity.

    Only the detector's findings reach S3 — the raw sign-ins stay in Coralogix
    — so this is the finding timeline, not a full activity log. It reports what
    it is rather than implying coverage it does not have.
    """
    doc = _entity_doc(t, entity_id)
    if doc is None:
        raise HttpError(404, f"no entity {entity_id!r} in the current snapshot")
    hours = _int(query, "hours", 48, 1, 24 * derive.WINDOW_DAYS)
    floor = _now_ms() - hours * 3_600_000
    anoms = [a for a in (doc.get("anomalies") or []) if (a.get("ts") or 0) >= floor]
    return {
        "events": [normalize.timeline_item(a, t.source_label) for a in anoms],
        "source": "detector findings only; raw sign-ins are not in the S3 store",
    }


# A Lambda Function URL refuses any response the runtime posts above 6,291,556
# bytes, and it fails the *whole* invocation with a 413 rather than returning a
# partial body — the caller sees a bare 502 with nothing in it. Feed records are
# the detector's findings verbatim (~6.5 KB each, carrying baseline context and
# event ids), so `limit=1000` serialises to ~6.6 MB and every dashboard load
# 502'd while the identical request against `local_server.py`, which has no such
# cap, succeeded. Budget on serialised size and report the trim honestly.
#
# `lambda_handler` also gzips when the client accepts it, which keeps a full feed
# an order of magnitude under the cap; this budget is the backstop for the
# clients that do not, and is what makes the route correct on its own.
MAX_FEED_BYTES = 5_000_000


def _fit_budget(rows: list) -> list:
    """The longest newest-first prefix of `rows` that fits MAX_FEED_BYTES."""
    kept, used = [], 0
    for row in rows:
        used += len(json.dumps(row, default=str))
        if used > MAX_FEED_BYTES:
            break
        kept.append(row)
    return kept


def anomalies(t: Tenant, query: dict) -> dict:
    """The anomaly feed for a rolling window.

    `days` is a rolling window ending now, matching the shell's own wording, not
    a count of calendar days — so `days=1` is the last 24 hours.
    """
    feed = _feed(t)
    all_rows = feed.get("anomalies") or []
    days = _int(query, "days", derive.WINDOW_DAYS, 1, derive.WINDOW_DAYS)
    floor = _now_ms() - days * DAY_MS
    rows = [a for a in all_rows if (a.get("ts") or 0) >= floor]

    ent = (query.get("entity") or "").strip().lower()
    det = (query.get("detection_id") or "").strip()
    if ent:
        rows = [a for a in rows if (a.get("entity") or "") == ent]
    if det:
        rows = [a for a in rows if a.get("detection_id") == det]

    # `total` has to be truthful about a feed that holds only the most recent
    # slice. The feed is sorted newest-first, so if its oldest record predates
    # the window, the window is entirely inside the feed and the count is exact.
    # Only a window reaching past the feed needs the builder's per-day tally,
    # and that tally counts whole calendar days — hence the estimate flag.
    oldest_in_feed = min((a.get("ts") or 0) for a in all_rows) if all_rows else 0
    covered = bool(all_rows) and floor >= oldest_in_feed
    estimate = False
    if covered or ent or det:
        total = len(rows)
    else:
        per_day = feed.get("per_day") or {}
        floor_day = derive._day_str(floor)
        total = sum(n for d, n in per_day.items() if d >= floor_day)
        # The boundary day is counted whole, so this can only over-report, and
        # only for a window wider than the feed.
        estimate = True

    limit = _int(query, "limit", 200, 1, config.FEED_SIZE)
    served = _fit_budget(rows[:limit])
    return {
        "anomalies": served,
        "total": total,
        # True whenever the caller is seeing fewer rows than the window holds,
        # whether `limit` or the byte budget did the cutting.
        "truncated": total > len(served),
        "total_is_estimate": estimate,
    }


def correlations(t: Tenant, _query: dict) -> dict:
    return _snapshot(t, "chains.json", default={
        "generated_at": "", "chains": [],
        "tactic_ranks": catalog.TACTIC_RANKS,
        "chain_alert_score": derive.CHAIN_ALERT_SCORE,
    }) or {}


def tuning(t: Tenant, _query: dict) -> dict:
    """The scoring parameters actually used. Read-only — there is no override.

    The shell reads `bands.notable` as its critical threshold and quotes
    `half_life_days` and `window_days` on the screens, so these must be the same
    constants `derive` scored with, not a copy that can drift.

    `severity_bands` is the weight -> severity ladder, served for the same
    reason: the UI colours an anomaly from its weight, and it used to carry its
    own copy of these cutoffs. Read straight off `catalog.SEVERITY_BANDS` (each
    band's `low`, the first weight that lands in it) rather than restated here,
    so an edit to the catalog cannot leave the UI painting the old bands.
    """
    return {
        "scoring": {
            "half_life_days": derive.HALF_LIFE_DAYS,
            "window_days": derive.WINDOW_DAYS,
            "bands": derive.BANDS,
            "severity_bands": {
                name: catalog.SEVERITY_BANDS[name][1]
                for name in ("critical", "error", "warning")
            },
        }
    }


def tenant_index(_query: dict) -> dict:
    """The customers this deployment serves, for the console's picker.

    Deliberately tenant-free: it is the one route that answers *about* tenants
    rather than *for* one, so it takes no `?tenant=` and needs none.

    `ready` is whether a snapshot exists — a tenant whose detector is running
    but whose builder has never produced one is a real and visible state, not
    an error. The console keeps such a tenant selectable and badges it, because
    hiding it would hide exactly the condition someone needs to act on.

    Never emits the bucket, the prefix or the secret ARN: this is a display
    contract, not the registry.
    """
    out = []
    for t in tenants.all():
        row = t.public()
        try:
            meta = _snapshot(t, "meta.json", default=None)
        except Exception as e:  # noqa: BLE001 - one unreadable tenant must not 500 the list
            log.warning("tenant %s: meta.json unreadable: %s", t.id, e)
            row.update(ready=False, error=f"{type(e).__name__}: {e}")
            out.append(row)
            continue
        meta = meta or {}
        row.update(
            ready=bool(meta),
            snapshot_generated_at=meta.get("generated_at"),
            snapshot_generated_at_ms=meta.get("generated_at_ms") or None,
            entities=meta.get("entities"),
            anomalies_30d=meta.get("anomalies"),
        )
        out.append(row)
    return {"default": tenants.default().id, "tenants": out}


# --- dispatch --------------------------------------------------------------

_ENTITY = r"(?P<id>[^/]+)"

# `/tenants` answers about the registry rather than out of one tenant's
# snapshot, so it is dispatched before tenant resolution and its handler takes
# no `Tenant`. Everything else does — see `handle`.
TENANT_FREE = "/tenants"

_ROUTES: list[tuple[str, str, object]] = [
    ("GET", r"^/tenants$", lambda m, q, b, t: tenant_index(q)),
    ("GET", r"^/health$", lambda m, q, b, t: health(t, q)),
    ("GET", r"^/overview$", lambda m, q, b, t: overview(t, q)),
    ("GET", r"^/notables$", lambda m, q, b, t: notables(t, q)),
    ("GET", r"^/entities$", lambda m, q, b, t: entities(t, q)),
    ("GET", rf"^/entities/{_ENTITY}/graph$", lambda m, q, b, t: entity_graph(t, unquote(m["id"]), q)),
    ("GET", rf"^/entities/{_ENTITY}/events$", lambda m, q, b, t: entity_events(t, unquote(m["id"]), q)),
    ("GET", rf"^/entities/{_ENTITY}$", lambda m, q, b, t: entity(t, unquote(m["id"]), q)),
    ("GET", r"^/anomalies$", lambda m, q, b, t: anomalies(t, q)),
    ("GET", r"^/correlations$", lambda m, q, b, t: correlations(t, q)),
    ("GET", r"^/tuning$", lambda m, q, b, t: tuning(t, q)),
]
_COMPILED = [(meth, re.compile(pat), fn) for meth, pat, fn in _ROUTES]


def handle(method: str, path: str, query: dict, body: dict | None,
           headers: dict | None = None) -> tuple[int, dict]:
    """Route one request. Returns (status, json-serialisable body)."""
    if config.API_KEY:
        got = ""
        for k, v in (headers or {}).items():
            if k.lower() == "x-api-key":
                got = v or ""
                break
        if got != config.API_KEY:
            return 403, {"error": "missing or invalid X-Api-Key"}

    # The UI calls `/api/...`; strip the prefix so routes read plainly.
    if path.startswith("/api"):
        path = path[4:]
    path = "/" + path.strip("/")

    # Which customer this request is for. After the API-key check, deliberately:
    # an unauthenticated caller must not be able to enumerate tenant ids from a
    # 400 body.
    tenant: Tenant | None = None
    if path != TENANT_FREE:
        asked = (query.get("tenant") or "").strip()
        if not asked:
            # Absent, not wrong. Every caller that predates the parameter — the
            # ops curl, deploy.sh's smoke test, a cached older UI bundle — was
            # already being served this tenant and cannot be surprised by it.
            tenant = tenants.default()
        else:
            try:
                tenant = tenants.get(asked)
            except tenants.UnknownTenant:
                # Never fall back to the default here. A caller that named a
                # specific customer and silently got a different one is how you
                # end up screenshotting the wrong company's accounts.
                return 400, {"error": f"unknown tenant {asked!r}",
                             "known": tenants.ids()}

    for meth, pat, fn in _COMPILED:
        m = pat.match(path)
        if not m:
            continue
        if meth != method.upper():
            return 405, {"error": f"{method} not allowed on {path}"}
        try:
            return 200, fn(m.groupdict(), query, body, tenant)
        except HttpError as e:
            return e.status, {"error": e.message}
        except Exception as e:  # noqa: BLE001 - one bad route must not 500 the app
            log.exception("handler failed for %s %s", method, path)
            return 500, {"error": f"{type(e).__name__}: {e}"}
    return 404, {"error": f"no route for {method} {path}"}
