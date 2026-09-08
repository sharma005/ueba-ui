"""Persist each detect run's findings to S3, alongside shipping them to Coralogix.

Deployed into the detector's package root, beside `s3_state.py`. The detector has
always shipped findings to Coralogix and kept only baseline state in S3, which
left the UI with no queryable finding store; this writes the same records the
ingest client is about to send, under a prefix the detector otherwise never
touches.

Records are stored **verbatim**. Normalization, weighting and the MITRE mapping
all live in the serving layer, so re-deriving any of them is a rebuild rather
than a re-pull, and this module never has to change when the console's
presentation does.

Contract with the caller (`src/cli.py::cmd_detect`):

  * Call **before** `CoralogixSinglesClient.send_batch`, which pops `_severity`
    off each record and would otherwise leave the archived copy missing the one
    field the console's severity colouring is derived from.
  * Wrap the call so a failure here can never affect detection or ingest. This
    module is best-effort telemetry, not part of the detection path.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import logging
from typing import Any, Iterable

import boto3

log = logging.getLogger(__name__)

# Matches serving/config.py. Findings are partitioned by the UTC day they
# describe, not the day the run happened: a 00:30 UTC run reports mostly on
# yesterday, and the console's day buckets have to line up with the events.
UI_SUBPREFIX = "ui/anomalies"


def _day_of(record: dict) -> str:
    """UTC day this finding describes, from the record's own timestamp."""
    ms = record.get("_timestamp_ms")
    if not isinstance(ms, (int, float)) or ms <= 0:
        secs = record.get("event_time_epoch")
        ms = (secs * 1000) if isinstance(secs, (int, float)) and secs > 0 else None
    if not ms:
        return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


def write_run(
    *,
    bucket: str,
    prefix: str,
    run_id: str,
    run_at_iso: str,
    anomalies: Iterable[dict[str, Any]],
    audit: dict[str, Any] | None = None,
    s3_client=None,
) -> list[str]:
    """Write this run's findings, partitioned by UTC day. Returns keys written.

    One object per (day, run) rather than appending to a day file: S3 has no
    append, and read-modify-write from a Lambda that can be retried would lose
    findings. The reader globs the day prefix and deduplicates on `incident_id`.
    """
    records = [a for a in (anomalies or []) if isinstance(a, dict)]
    by_day: dict[str, list[dict]] = collections.defaultdict(list)
    for rec in records:
        by_day[_day_of(rec)].append(rec)

    # The run summary is telemetry about the run itself, so it belongs to the
    # day the run happened. `/health` reads these to report pipeline state.
    if audit:
        by_day[_day_of({"_timestamp_ms": None})].append(audit)

    s3 = s3_client or boto3.client("s3")
    base = f"{prefix.strip('/')}/{UI_SUBPREFIX}" if prefix else UI_SUBPREFIX
    safe_run = "".join(c for c in str(run_id) if c.isalnum() or c in "-_")[:64] or "run"

    written: list[str] = []
    for day, recs in sorted(by_day.items()):
        key = f"{base}/dt={day}/run-{safe_run}.json"
        body = json.dumps(recs, separators=(",", ":"), default=str).encode()
        s3.put_object(Bucket=bucket, Key=key, Body=body,
                      ContentType="application/json")
        written.append(key)
        log.info("ui sink wrote %d record(s) -> s3://%s/%s (%d bytes)",
                 len(recs), bucket, key, len(body))

    log.info("ui sink: %d finding(s) across %d day(s) for run %s at %s",
             len(records), len(by_day), safe_run, run_at_iso)
    return written
