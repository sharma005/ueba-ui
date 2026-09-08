#!/usr/bin/env python3
"""One-time seed: pull the detector's shipped findings out of Coralogix into S3.

The detector has always shipped to Coralogix and never to S3, so on day one the
S3 anomaly store is empty and the UI would show 30 days of nothing until the
patched detector filled it in. This walks the anomaly stream a day at a time and
writes the same layout the patched detector writes, so the two are
interchangeable and the builder cannot tell which produced a given day.

Records are stored **exactly as the detector emitted them** — normalization is
the builder's job, so re-deriving weights or the catalog never needs a re-pull.
Telemetry records (`run_summary`, `baseline_health`) are kept too: they are what
`/health` reports the pipeline's own state from.

    lambdas/api/.venv/bin/python lambdas/api/backfill.py --days 30 \
        --profile snowbit-research

Idempotent: a day already present is skipped unless `--force`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ANOMALY_STREAM = (
    "source logs"
    " | filter $l.applicationname == '{app}'"
    "   && $l.subsystemname == '{sub}'"
)

log = logging.getLogger("backfill")


def load_secret(session, arn: str) -> dict:
    payload = json.loads(
        session.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"]
    )
    missing = [
        k for k in ("query_api_key", "dataprime_url")
        if not payload.get(k) or str(payload[k]).startswith("REPLACE_WITH")
    ]
    if missing:
        raise SystemExit(f"secret {arn} is missing {missing}")
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30,
                    help="how many days back to pull (default 30, the scoring window)")
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE_OVERRIDE"),
                    help="AWS profile for S3 and Secrets Manager")
    ap.add_argument("--tenant", default=None,
                    help="which customer to backfill; defaults to the default tenant")
    # Default from the tenant, not from the parser: an Azure app/subsystem pair
    # silently matches nothing against an Okta stream, and `read_coralogix`
    # would report that as "every finding is missing".
    ap.add_argument("--app", default=None)
    ap.add_argument("--subsystem", default=None)
    # Archive is the right default — it is the only tier that reaches back 30
    # days — but it is also the tier currently failing for some days on the eu1
    # cluster, so it has to be overridable. Note that `$l.applicationname` does
    # not resolve on TIER_FREQUENT_SEARCH (see reconcile.py), so a fallback run
    # there needs the filter adjusting rather than trusting a zero result.
    ap.add_argument("--tier", default="TIER_ARCHIVE",
                    choices=("TIER_ARCHIVE", "TIER_FREQUENT_SEARCH"))
    ap.add_argument("--force", action="store_true",
                    help="re-pull days already present in S3")
    ap.add_argument("--dry-run", action="store_true",
                    help="query and report counts, write nothing")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.profile:
        os.environ["AWS_PROFILE_OVERRIDE"] = args.profile

    import boto3
    from serving import catalog, config, s3store, tenants
    from serving.coralogix import DataPrimeClient

    t = tenants.get(args.tenant) if args.tenant else tenants.default()
    app = args.app or t.coralogix_anomaly_app
    subsystem = args.subsystem or t.coralogix_anomaly_subsystem
    log.info("backfilling %s from %s/%s into s3://%s/%s",
             t.id, app, subsystem, t.bucket, t.anomaly_prefix)

    session = (boto3.Session(profile_name=args.profile, region_name=config.REGION)
               if args.profile else boto3.Session(region_name=config.REGION))
    sec = load_secret(session, t.coralogix_secret_arn)
    dp = DataPrimeClient(sec["dataprime_url"], sec["query_api_key"], timeout=540)
    query = ANOMALY_STREAM.format(app=app, sub=subsystem)

    existing = set()
    if not args.force:
        existing = {
            k.split("/dt=")[1].split("/")[0]
            for k in s3store.list_keys(t.bucket, t.anomaly_prefix + "/")
            if "/dt=" in k
        }
        if existing:
            log.info("already present: %d day(s)", len(existing))

    today = dt.datetime.now(dt.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    total = 0
    findings = 0
    written_days = 0
    failed_days: list[str] = []

    # Newest first: if the pull is interrupted the UI already has the days an
    # analyst is most likely to look at.
    for i in range(args.days):
        day_start = today - dt.timedelta(days=i)
        day = day_start.strftime("%Y-%m-%d")
        if day in existing:
            log.info("%s  skip (present)", day)
            continue
        day_end = day_start + dt.timedelta(days=1)

        # One day's failure must not cost the rest. Coralogix's archive tier
        # returns a bare `Failed to run the query …/archive/…` for some days —
        # the same error that has been breaking the Okta detector's own baseline
        # fetch — and aborting here threw away 29 good days for one bad one.
        try:
            rows = list(dp.query(
                query,
                start_iso=day_start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                end_iso=day_end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                tier=args.tier,
                default_limit=50000,
            ))
        except Exception as e:  # noqa: BLE001 - reported per day, not raised
            log.warning("%s  QUERY FAILED, skipping this day: %s", day, e)
            failed_days.append(day)
            continue

        kinds: dict[str, int] = {}
        for r in rows:
            k = r.get("detection_kind") or "?"
            kinds[k] = kinds.get(k, 0) + 1
        real = sum(n for k, n in kinds.items()
                   if k not in catalog.TELEMETRY_KINDS and k != "?")
        total += len(rows)
        findings += real
        log.info("%s  %5d records (%d findings)  %s", day, len(rows), real,
                 ", ".join(f"{k.replace('azure_ad_', '')}={n}"
                           for k, n in sorted(kinds.items(), key=lambda kv: -kv[1])))

        if args.dry_run or not rows:
            continue
        key = f"{t.anomaly_prefix}/dt={day}/backfill.json"
        size = s3store.put_json(t.bucket, key, rows)
        written_days += 1
        log.info("%s  -> s3://%s/%s (%.1f MB)", day, t.bucket, key, size / 1e6)

    log.info("done: %d records (%d findings) across %d day(s) written",
             total, findings, written_days)
    if failed_days:
        # Named, not just counted: a day missing from the store is a hole in
        # every count the console derives from it, and the only way anyone
        # learns which days are affected is if this says so.
        log.warning("%d day(s) could not be queried and are ABSENT from the "
                    "store: %s", len(failed_days), ", ".join(sorted(failed_days)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
