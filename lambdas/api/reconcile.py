#!/usr/bin/env python3
"""Account for every record between the Coralogix log stream and the UI's count.

The console shows "N anomalies, last 24h"; the same window in Coralogix Explore
shows a larger number of log lines. Four things sit in between — telemetry
records that are not findings, deduplication on `incident_id`, records the
normalizer cannot place, and the gap between the snapshot's build time and the
request's rolling window — and none of them are visible from outside. This walks
all four and prints a ledger that has to sum, so the difference is either fully
attributed or it names a bug.

Read-only: it queries Coralogix and GETs from S3, and writes nothing anywhere.

    lambdas/api/.venv/bin/python lambdas/api/reconcile.py \
        --profile snowbit-research --hours 24

This stream lives only in Coralogix's archive tier: on the frequent-search tier
`$l.applicationname` does not resolve and the query silently matches nothing,
so `--tier` defaults to `TIER_ARCHIVE` and an empty result is raised rather than
reported as a column of zeroes.

Both sources are put through the *same* filter chain — `serving.catalog` and
`serving.normalize`, imported rather than reimplemented, so this cannot drift
from what `serving/build.py` actually does. The two "records read" lines are
therefore not comparable to each other (one is a log-time query, the other whole
UTC day partitions); the line that matters is "findings in window", which is.

Two faithful-to-the-builder quirks, both deliberate:

  * Dedup is first-wins in ascending key order, exactly as `build.load_findings`
    iterates. If the detector ever re-emits one `incident_id` with a *different*
    `_timestamp_ms`, both the builder and this script keep the older copy.
  * Dedup here runs only over the day partitions the window touches, where the
    builder dedups across its whole 30-day read. A duplicate pair straddling
    that boundary would show up as `unexplained` rather than being hidden.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import datetime as dt
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

log = logging.getLogger("reconcile")

# The stages `build.load_findings` applies, in the order it applies them. The
# ledger prints them in this order, and they must sum back to `read`.
DROP_STAGES = (
    ("telemetry", "telemetry"),
    ("no_incident_id", "no incident_id"),
    ("no_timestamp", "no timestamp"),
    ("duplicate_incident_id", "duplicate incident_id"),
    ("outside_window", "outside event-time window"),
)


@dataclasses.dataclass
class Stages:
    """What happened to each record on the way to the feed."""

    read: int = 0
    telemetry: int = 0
    no_incident_id: int = 0
    no_timestamp: int = 0
    duplicate_incident_id: int = 0
    outside_window: int = 0
    kept: list = dataclasses.field(default_factory=list)
    kinds: collections.Counter = dataclasses.field(default_factory=collections.Counter)

    @property
    def kept_n(self) -> int:
        return len(self.kept)

    @property
    def dropped(self) -> int:
        return sum(getattr(self, f) for f, _ in DROP_STAGES)

    def balances(self) -> bool:
        return self.read == self.dropped + self.kept_n

    def as_dict(self) -> dict:
        d = {"read": self.read, "findings_in_window": self.kept_n}
        d.update({f: getattr(self, f) for f, _ in DROP_STAGES})
        d["by_detection_kind"] = dict(self.kinds.most_common())
        return d


def classify(records, floor_ms: int, lk) -> Stages:
    """Walk detector records through the builder's filter chain, counting drops.

    Pure: no S3, no network, no clock. `records` is whatever the log stream or
    the raw store yielded, verbatim as the detector emitted it.
    """
    from serving import catalog, normalize

    st = Stages()
    seen: set[str] = set()
    for rec in records:
        # `build.load_findings` skips non-dicts before it counts, so this does too.
        if not isinstance(rec, dict):
            continue
        st.read += 1
        if rec.get("detection_kind") in catalog.TELEMETRY_KINDS:
            st.telemetry += 1
            continue
        a = normalize.anomaly(rec, lk)
        if not a:
            st.no_incident_id += 1
            continue
        if not a["ts"]:
            st.no_timestamp += 1
            continue
        if a["anomaly_id"] in seen:
            st.duplicate_incident_id += 1
            continue
        seen.add(a["anomaly_id"])
        if a["ts"] < floor_ms:
            st.outside_window += 1
            continue
        st.kept.append(a)
        st.kinds[rec.get("detection_kind") or "?"] += 1
    return st


# --- Sources ---------------------------------------------------------------


def read_s3_raw(t, floor_ms: int) -> tuple[list[dict], list[str]]:
    """Every raw record in the `dt=` partitions the window touches.

    Findings are partitioned by the UTC day they *describe*, which is the same
    basis as the `ts` the window is applied to, so reading from the floor's day
    forward is complete. It is also a superset: records earlier in the floor day
    are read and then counted under `outside_window`.
    """
    from serving import s3store

    floor_day = _day(floor_ms)
    keys = sorted(
        k for k in s3store.list_keys(t.bucket, t.anomaly_prefix + "/")
        if "/dt=" in k and k.split("/dt=")[1].split("/")[0] >= floor_day
    )
    records: list[dict] = []
    for blob in s3store.get_many_json(t.bucket, keys):
        if not blob:
            continue
        records.extend(blob if isinstance(blob, list) else blob.get("anomalies") or [])
    return records, keys


def read_coralogix(t, session, args, floor_ms: int, now_ms: int) -> list[dict]:
    """The same stream Explore shows, over the same rolling window."""
    import backfill
    from serving.coralogix import DataPrimeClient

    sec = backfill.load_secret(session, t.coralogix_secret_arn)
    dp = DataPrimeClient(sec["dataprime_url"], sec["query_api_key"], timeout=540)
    query = backfill.ANOMALY_STREAM.format(
        app=args.app or t.coralogix_anomaly_app,
        sub=args.subsystem or t.coralogix_anomaly_subsystem,
    )
    rows = list(dp.query(
        query,
        start_iso=_iso(floor_ms, millis=True),
        end_iso=_iso(now_ms, millis=True),
        tier=args.tier,
        default_limit=args.limit,
    ))
    if len(rows) >= args.limit:
        log.warning("coralogix returned %d rows at the query limit — raise --limit",
                    len(rows))
    if not rows:
        # Zero rows is nearly always the query, not the data: `$l.applicationname`
        # does not resolve on the frequent-search tier and silently matches
        # nothing. Reporting it as "every finding is missing from the log stream"
        # would be the most misleading output this script could produce.
        raise RuntimeError(
            f"query matched nothing on {args.tier} — check --tier/--app/--subsystem "
            f"rather than trusting a zero column")
    return rows


def read_snapshot(t, floor_ms: int) -> dict:
    """What the API would serve right now, and how stale it is."""
    from serving import s3store

    meta = s3store.get_json(t.bucket, t.snapshot_key("meta.json"), default={}) or {}
    feed = s3store.get_json(t.bucket, t.feed_key, default={}) or {}
    rows = feed.get("anomalies") or []
    in_window = [a for a in rows if (a.get("ts") or 0) >= floor_ms]
    oldest = min((a.get("ts") or 0) for a in rows) if rows else 0
    return {
        "built_ms": meta.get("generated_at_ms") or 0,
        "built_at": meta.get("generated_at"),
        "feed_rows": len(rows),
        "feed_total": feed.get("total"),
        "feed_truncated": bool(feed.get("truncated")),
        "in_window": len(in_window),
        # routes.anomalies() reports an exact `total` only when the window sits
        # inside the feed; otherwise it falls back to whole-calendar-day tallies.
        "window_covered": bool(rows) and floor_ms >= oldest,
        "oldest_ms": oldest,
    }


# --- Output ----------------------------------------------------------------


def _iso(ms: int, millis: bool = False) -> str:
    d = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.000Z" if millis else "%Y-%m-%dT%H:%M:%SZ")


def _day(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


def _age(ms: int) -> str:
    if ms <= 0:
        return "?"
    mins = int(ms / 60_000)
    return f"{mins}m ago" if mins < 120 else f"{mins // 60}h{mins % 60:02d}m ago"


def print_ledger(cx: Stages | None, s3: Stages, snap: dict,
                 lag: int, floor_ms: int, now_ms: int, hours: int) -> int:
    """The ledger. Returns the unexplained residual."""
    w, c1, c2 = 44, 11, 11
    two = cx is not None

    print()
    print(f"window   {_iso(floor_ms)} .. {_iso(now_ms)}  ({hours}h, event time)")
    print()
    head = f"{'':<{w}}{'coralogix':>{c1}}" if two else f"{'':<{w}}"
    print(head + f"{'s3 raw':>{c2}}")
    print("-" * (w + (c1 if two else 0) + c2))

    def row(label, a, b, indent=0):
        lab = " " * indent + label
        left = f"{a:>{c1}}" if two else ""
        print(f"{lab:<{w}}{left}{b:>{c2}}")

    row("records read", cx.read if two else None, s3.read)
    for field, label in DROP_STAGES:
        row(f"- {label}",
            f"-{getattr(cx, field)}" if two else None,
            f"-{getattr(s3, field)}", indent=2)
    row("findings in window", cx.kept_n if two else None, s3.kept_n)

    if two:
        delta = cx.kept_n - s3.kept_n
        note = "the log stream and the raw store agree" if delta == 0 else (
            f"{abs(delta)} finding(s) in the log stream never reached S3"
            if delta > 0 else f"{abs(delta)} finding(s) in S3 are not in the log query")
        print(f"{'':<{w}}{'delta ' + f'{delta:+d}':>{c1 + c2}}   {note}")

    for st, name in ((cx, "coralogix"), (s3, "s3 raw")):
        if st is not None and not st.balances():
            print(f"  !! {name} ledger does not balance "
                  f"({st.read} read vs {st.dropped}+{st.kept_n})")

    print()
    print("snapshot")
    built = snap["built_ms"]
    print(f"  built                       {snap['built_at'] or '?'}"
          f"  ({_age(now_ms - built) if built else 'never built'})")
    print(f"  feed rows / total           {snap['feed_rows']} / {snap['feed_total']}"
          f"{'  TRUNCATED' if snap['feed_truncated'] else ''}")
    print(f"  window inside the feed      {'yes' if snap['window_covered'] else 'NO'}"
          f"{'' if snap['window_covered'] else '  -> the API total is an estimate'}")
    print(f"  feed rows in window         {snap['in_window']}   <- what the UI shows")
    print(f"  raw findings after build    {lag}   <- snapshot lag, not yet in the feed")

    residual = s3.kept_n - lag - snap["in_window"]
    print(f"  unexplained                 {residual}")
    print()
    if residual == 0:
        print("Every record is accounted for: the gap is telemetry, dedup, the "
              "event-time window, and snapshot lag.")
    else:
        print(f"{abs(residual)} finding(s) unaccounted for. The raw store and the "
              "feed disagree beyond snapshot lag — rebuild the snapshot and re-run; "
              "if it persists, the builder is dropping records this chain does not model.")
    return residual


def print_kinds(cx: Stages | None, s3: Stages, top: int) -> None:
    """Per-detection breakdown of what survived, on both sources."""
    from serving import catalog

    # The donut column is the S3 side only, so it is one column wide either way.
    def table(title: str, rows: list[tuple[str, int, int | None]]) -> None:
        print()
        print(title)
        for label, right, left in rows:
            lcol = f"{left:>11}" if left is not None else ""
            print(f"  {label:<42}{lcol}{right:>11}")

    kinds = sorted(set(s3.kinds) | set(cx.kinds if cx else ()),
                   key=lambda k: -s3.kinds[k])[:top]
    table(f"findings in window by detection_kind (top {top})",
          [(k.replace("azure_ad_", ""), s3.kinds[k],
            cx.kinds[k] if cx else None) for k in kinds])

    # What the dashboard donut actually groups by — the primary reason kind, not
    # the record-level kind above. Both are raw log values, but the reason kind
    # is far more granular (~17 values against 5), so the two tables differ.
    donut: collections.Counter = collections.Counter(
        a["detection_id"] for a in s3.kept)
    table(f"...by dashboard donut slice (top {top})",
          [(catalog.label(k), n, None) for k, n in donut.most_common(top)])


# --- Entry point -----------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=int, default=24,
                    help="rolling window ending now (default 24, the dashboard's)")
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE_OVERRIDE"),
                    help="AWS profile for S3 and Secrets Manager")
    ap.add_argument("--tenant", default=None,
                    help="which customer to reconcile; defaults to the default tenant")
    # Default from the tenant — see backfill.py for why these must not be
    # parser-level Azure literals.
    ap.add_argument("--app", default=None)
    ap.add_argument("--subsystem", default=None)
    # This stream is archive-only: the frequent-search tier returns zero rows
    # for it on either keypath, so `$l.applicationname` + archive (what the
    # backfill has always used) is the only combination that sees the data.
    ap.add_argument("--tier", default="TIER_ARCHIVE",
                    choices=("TIER_ARCHIVE", "TIER_FREQUENT_SEARCH"),
                    help="TIER_ARCHIVE is the only tier this stream is in")
    ap.add_argument("--limit", type=int, default=50000, help="DataPrime row limit")
    ap.add_argument("--no-coralogix", action="store_true",
                    help="reconcile the raw store against the feed only")
    ap.add_argument("--top", type=int, default=10, help="rows per breakdown table")
    ap.add_argument("--json", action="store_true", help="emit the ledger as JSON too")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.profile:
        os.environ["AWS_PROFILE_OVERRIDE"] = args.profile

    import boto3
    from serving import config, derive, links, tenants

    t = tenants.get(args.tenant) if args.tenant else tenants.default()
    lk = links.for_tenant(t)
    log.info("reconciling %s against s3://%s/%s", t.id, t.bucket, t.state_prefix)

    if args.hours > derive.WINDOW_DAYS * 24:
        log.warning("--hours %d reaches past the builder's %d-day window; the feed "
                    "cannot cover it", args.hours, derive.WINDOW_DAYS)

    now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    floor_ms = now_ms - args.hours * 3_600_000

    session = (boto3.Session(profile_name=args.profile, region_name=config.REGION)
               if args.profile else boto3.Session(region_name=config.REGION))

    raw, keys = read_s3_raw(t, floor_ms)
    log.info("s3: %d record(s) from %d object(s) across %s",
             len(raw), len(keys),
             ", ".join(sorted({k.split("/dt=")[1].split("/")[0] for k in keys})) or "-")
    s3_stages = classify(raw, floor_ms, lk)

    cx_stages = None
    cx_rows: list[dict] = []
    if not args.no_coralogix:
        try:
            cx_rows = read_coralogix(t, session, args, floor_ms, now_ms)
            log.info("coralogix: %d row(s) over %s", len(cx_rows), args.tier)
            cx_stages = classify(cx_rows, floor_ms, lk)
        except Exception as e:  # noqa: BLE001
            # The S3 half is the useful half and needs no Coralogix credentials;
            # losing the log-stream column must not cost the whole run.
            log.warning("coralogix query failed, continuing with S3 only: %s", e)

    snap = read_snapshot(t, floor_ms)
    built = snap["built_ms"]
    lag = sum(1 for a in s3_stages.kept if a["ts"] > built) if built else 0

    residual = print_ledger(cx_stages, s3_stages, snap, lag, floor_ms, now_ms, args.hours)
    print_kinds(cx_stages, s3_stages, args.top)

    # How many log-time rows fall outside the event-time window: the clock-basis
    # difference, which is the one nobody expects when comparing against Explore.
    if cx_rows:
        from serving import normalize
        outside = sum(1 for r in cx_rows if isinstance(r, dict)
                      and 0 < normalize.ts_ms(r) < floor_ms)
        print()
        print(f"clock basis: {outside} of {len(cx_rows)} log rows are inside "
              f"Coralogix's log-time window but describe events older than it")

    if args.json:
        print()
        print(json.dumps({
            "window": {"floor_ms": floor_ms, "now_ms": now_ms, "hours": args.hours},
            "coralogix": cx_stages.as_dict() if cx_stages else None,
            "s3_raw": s3_stages.as_dict(),
            "snapshot": snap,
            "snapshot_lag_findings": lag,
            "unexplained": residual,
        }, indent=2, default=str))

    # Always 0: this is a diagnostic, not a gate. The residual is in the output.
    return 0


if __name__ == "__main__":
    sys.exit(main())
