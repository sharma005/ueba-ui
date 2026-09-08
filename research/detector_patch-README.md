# Detector patch — persist findings to S3

The detector `azuread-anomaly-detector-iforest-ap2-v3` ships every finding to
Coralogix and keeps only two files in S3 (`baseline.json`,
`detector_state.json`). The UEBA console needs a queryable finding store, so
this patch makes each detect run also write its findings to S3, under a prefix
the detector otherwise never touches.

It is two changes and nothing else:

1. **New file** `s3_anomaly_sink.py` in the package root, beside `s3_state.py`.
2. **Six lines** in `src/cli.py::cmd_detect`, immediately before
   `sink.send_batch(anomalies)`.

## The call site

```python
    # Persist this run's findings to S3 for the UEBA console, which has no other
    # queryable store of them. Deliberately before send_batch: that call pops
    # `_severity` off each record in place, and the console derives an anomaly's
    # weight from it. Best-effort — a sink failure must never cost us the ingest.
    try:
        from s3_anomaly_sink import write_run

        write_run(
            bucket=os.environ["STATE_BUCKET"],
            prefix=os.environ.get("STATE_PREFIX") or f"tenants/{cfg.get('tenant_id')}",
            run_id=audit_record["run_id"],
            run_at_iso=run_at_iso,
            anomalies=anomalies,
            audit=audit_record,
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("ui anomaly sink failed, continuing to ingest: %s", e)

    sent_anom = sink.send_batch(anomalies)
```

Two properties matter, and both are load-bearing:

- **Ordering.** `CoralogixSinglesClient.send_batch` does `rec.pop("_severity")`,
  mutating each record in place. Writing after it would archive records missing
  the one field the console's severity colouring is derived from.
- **Isolation.** The whole call is wrapped. This is best-effort telemetry, never
  part of the detection path: if S3 is unavailable, detection and ingest must
  proceed exactly as before.

It also sits *after* the `args.dry_run` early return, so a dry run still writes
nothing anywhere.

## What it writes

```
s3://azuread-anomaly-iforest-state-119418761367-ap2/
  tenants/jiostar/ui/anomalies/dt=YYYY-MM-DD/run-<run_id>.json
```

Records are stored **verbatim** — normalization, weighting and the MITRE mapping
all live in the serving layer, so re-deriving any of them is a rebuild rather
than a re-pull. The run summary is kept too; `/api/health` reports pipeline
state from it.

Partitioned by the UTC day each finding *describes*, not the day the run
happened: a 00:30 UTC run reports mostly on yesterday, and the console's day
buckets have to line up with the events. One object per (day, run) because S3
has no append and read-modify-write from a retryable Lambda would lose
findings; the reader globs the day prefix and deduplicates on `incident_id`.

No IAM change was needed — `azuread-anomaly-iforest-lambda-role-ap2` already
grants `s3:PutObject` across the whole bucket.

## Deployment, and the caveat

The detector's source repo is not in this workspace, so the patch was applied to
the **deployed artifact**: download the zip, add the module, edit `src/cli.py`,
re-zip, `update-function-code`. Verified `diff -rq` against a pristine
extraction: one added file, one modified file, nothing else.

**If you have the detector's own repo, land the same six lines there and deploy
from it** — otherwise the next deploy from that repo silently reverts this.

Applied 2026-08-31:

| | |
|---|---|
| Rollback version | **12** (`aws lambda update-function-code --function-name azuread-anomaly-detector-iforest-ap2-v3 --s3-...` or redeploy version 12's code) |
| SHA before | `m1hyKC5AUDwNtjTbPDZtFRXSIjhmJq51uOI+s/eMRKQ=` |
| SHA after | `sdQ0xYOkM7WUBf4pCZNlu0RSXKii0s/lItbFt1XMiwQ=` |

Version 12 is a published snapshot of the pre-patch code, so rollback is
restoring that version's code to `$LATEST` and confirming the SHA matches the
"before" value above.

## Verification performed

- `detect_ok: true` on two post-patch invocations; the run summary still reports
  `anomalies_shipped`, so ingest is unaffected.
- Run objects landed at `dt=2026-08-31/run-<id>.json` and the sink logged each
  write.
- Both test invocations legitimately found **zero** findings (the 12:30
  scheduled run had already deduplicated that window), so the findings path is
  covered by `lambdas/api/tests/test_detector_sink.py` — day partitioning, the
  `_severity` round-trip, spray findings that carry no `event_time_epoch`,
  multi-day splits, and run-id sanitisation.

## Housekeeping

The store grows by ~13 objects a day and the builder only reads the last 30
days, so `deploy.sh` installs an S3 lifecycle rule expiring
`tenants/jiostar/ui/anomalies/` at 45 days. It is scoped to that prefix;
detector state is untouched.
