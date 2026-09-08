#!/usr/bin/env python3
"""Snapshot builder entrypoint — Lambda handler and CLI in one file.

    lambdas/api/.venv/bin/python lambdas/api/snapshot.py --profile snowbit-research
    lambdas/api/.venv/bin/python lambdas/api/snapshot.py --tenant deel
    lambdas/api/.venv/bin/python lambdas/api/snapshot.py --all

As a Lambda it is triggered by S3 `ObjectCreated` on a detector's
`baseline/baseline.json`, so a rebuild follows every detector run without a
schedule to drift out of step with it.

One function serves every tenant. The event names the bucket and key, which is
enough to say whose baseline landed — see `_tenants_from_event`. An event no
tenant claims is logged and skipped rather than falling back to the default,
because a rebuild triggered by one customer must never overwrite another's
snapshot.
"""

from __future__ import annotations

import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

log = logging.getLogger("snapshot")


def _tenants_from_event(event) -> list:
    """Which tenants an invocation is asking to build.

    Four shapes, in precedence order:

      S3 notification      -> the tenant owning each record's (bucket, key)
      {"tenant": "deel"}   -> that one
      {"tenants": [...]}   -> those, in the order given
      {"all": true}        -> every registered tenant
      {}                   -> the default, preserving the pre-existing
                              `aws lambda invoke` with an empty payload
    """
    from serving import tenants

    event = event or {}

    records = event.get("Records") or []
    if records:
        out, seen = [], set()
        for rec in records:
            s3 = (rec or {}).get("s3") or {}
            bucket = ((s3.get("bucket") or {}).get("name")) or ""
            key = ((s3.get("object") or {}).get("key")) or ""
            t = tenants.resolve_s3(bucket, key)
            if t is None:
                # Not ours, or a prefix no tenant claims. Never guess.
                log.warning("ignoring S3 event for unclaimed object s3://%s/%s",
                            bucket, key)
                continue
            if t.id not in seen:
                seen.add(t.id)
                out.append(t)
        return out

    if event.get("all"):
        return tenants.all()
    named = event.get("tenants") or ([event["tenant"]] if event.get("tenant") else [])
    if named:
        return [tenants.get(str(n)) for n in named]
    return [tenants.default()]


def _build_each(targets: list) -> dict:
    """Build each tenant in turn, isolating failures.

    Sequential, never parallel: `load_baseline` streams a ~150 MB document into
    a 1024 MB `/tmp` and holds it parsed in memory. Two at once would contend
    for both. The S3-trigger path always has exactly one target anyway; only an
    explicit `--all` has more.

    One tenant's failure must not cost another's build, so each is caught and
    reported rather than aborting the invocation.
    """
    from serving.build import build

    results, errors = {}, {}
    for t in targets:
        try:
            meta = build(t)
            results[t.id] = {k: v for k, v in meta.items() if k != "baseline"}
        except Exception as e:  # noqa: BLE001 - reported per tenant, not raised
            log.exception("snapshot build failed for %s", t.id)
            errors[t.id] = f"{type(e).__name__}: {e}"
    out: dict = {"results": results}
    if errors:
        out["errors"] = errors
    return out


def handler(event, context):
    targets = _tenants_from_event(event)
    if not targets:
        return {"results": {}, "note": "no tenant matched this event"}
    out = _build_each(targets)
    # A single-tenant invocation keeps its old flat shape, so the existing
    # `aws lambda invoke ... /dev/stdout` smoke test reads the same as before.
    if len(targets) == 1 and not out.get("errors"):
        return out["results"][targets[0].id]
    return out


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE_OVERRIDE"))
    ap.add_argument("--tenant", action="append", default=None,
                    help="tenant id to build; repeatable. Defaults to the default tenant.")
    ap.add_argument("--all", action="store_true", help="build every registered tenant")
    args = ap.parse_args()
    if args.profile:
        os.environ["AWS_PROFILE_OVERRIDE"] = args.profile

    from serving import tenants

    event: dict = {}
    if args.all:
        event = {"all": True}
    elif args.tenant:
        event = {"tenants": args.tenant}

    try:
        targets = _tenants_from_event(event)
    except tenants.UnknownTenant as e:
        print(f"unknown tenant {e.args[0]!r}; known: {', '.join(tenants.ids())}",
              file=sys.stderr)
        return 2

    out = _build_each(targets)
    print(json.dumps(out, indent=2, default=str))
    return 1 if out.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
