# CX UEBA console

A React console over the Azure AD anomaly detector running in AWS account
**snowbit-research** (119418761367), region **ap-south-1**, tenant **jiostar**.

```
Azure AD SignInLogs (Coralogix)
        │
        ▼
azuread-anomaly-detector-iforest-ap2-v3        every 2h, EventBridge
  ├── ships findings ──────────────────────►   Coralogix  (unchanged)
  ├── writes baseline.json (150 MB) ────────►  S3  tenants/jiostar/baseline/
  └── writes findings verbatim ─────────────►  S3  tenants/jiostar/ui/anomalies/dt=…/
                                                    ▲ added by lambdas/detector_patch
        │ S3 ObjectCreated on baseline.json
        ▼
ueba-ui-snapshot-ap2        3008 MB / 300 s, ~8 s per build
  parses the baseline, normalizes 30 days of findings, derives entity scores
  and attack chains, writes ~12 MB of small JSON
        │
        ▼
S3  tenants/jiostar/ui/snapshot/v1/            overview, notables, entities,
        │                                      feed, chains, 256 entity shards
        ▼
ueba-ui-api-ap2             512 MB / 30 s, serves /api/* from cached objects
                            read-only: 10 GET routes, no mutation path
        │
        ▼
ui/  (React + Vite)
```

## Why a snapshot

`baseline.json` is 150 MB and 30 days of findings is another 90 MB. Neither can
be touched on a request path. All the expensive work happens once per detector
cycle; the API only ever reads small precomputed objects, cached in-process by
ETag. An entity page is a single ~160 KB gzipped shard read.

## Running it locally

```bash
# 1. API — reads S3 directly with your own credentials
lambdas/api/.venv/bin/python lambdas/api/local_server.py 8787

# 2. UI — vite.config.ts already proxies /api to :8787
cd ui && npm run dev
```

`local_server.py` and the deployed Lambda share `serving/routes.py`, so a
response locally is the response deployed; only the transport differs.

The venv is for local work only — the Lambda runtime already ships boto3, and
the two modules that need `requests` (`backfill.py`, `serving/coralogix.py`) are
excluded from the deployment package.

## Deploying

```bash
lambdas/api/deploy.sh --profile snowbit-research --region ap-south-1
```

Idempotent: one IAM role, two Lambdas, the Function URL, the S3 trigger, and a
45-day lifecycle rule on the raw finding store. It refuses to proceed if the
bucket has a notification configuration it did not write, because
`put-bucket-notification-configuration` replaces the whole document.

> **Function URL auth.** This account blocks unauthenticated Lambda Function
> URLs — `AuthType: NONE` returns AWS's own 403 before the function is reached.
> The URL is therefore `AWS_IAM`, which a browser cannot call directly. Reaching
> the deployed API from a browser needs a front door that signs with SigV4
> (CloudFront + OAC is the usual one, and the same distribution can serve the UI
> from the private bucket). Until then the local server is the way in.

## Scoring, in one place

`serving/derive.py` owns it, and the numbers it uses are served read-only at
`/api/tuning` so the UI quotes the same constants it scored with.

- **Decay** — a finding's weight halves every 7 days, over a 30-day window that
  matches the detector's own baseline lookback.
- **Per-detection saturation** — repetition of one detection cannot escalate an
  account. Measured on real data: a service account with 34 daily `legacy_auth`
  findings and nothing else was scoring a maximum 100, level with an account
  under an active credential attack. Each kind now saturates below the notable
  band, so breadth across detections is what escalates.
- **Bands** — notable ≥ 90, high ≥ 70, medium ≥ 45, matching `theme.ts`'s
  `levelOf` so an API band and the colour the UI paints cannot disagree.
- **Weights** are anchored to the detector's own severity band, because the
  Anomaly Feed's severity filter is driven by `weightToSev(weight)` alone. A
  finding the detector called critical always paints critical.
  `tests/test_catalog.py` asserts it for every kind in every band.

`serving/catalog.py` maps all 22 detector reason kinds to a display name, a
weight bump and a MITRE tactic/technique — the detector emits no MITRE fields.
Only 15 of those kinds appear as literals in its `detect.py`; the rest are built
by its `_rarity_reason` helper. A live test checks the S3 stream against the
catalog so a kind added upstream fails a test instead of silently mislabelling.

## What this data cannot show

Called out because the UI has honest empty states for each, not fabrications:

- **Hosts.** Azure AD sign-ins name no host entity. `hosts_monitored` is 0 and
  `/entities?type=host` is empty.
- **Peer cohorts.** No behavioural clusterer runs in this pipeline; each account
  is compared against its own 30-day baseline. `peer_group` and `peer_stats` are
  null.
- **Raw sign-ins.** Only the detector's findings reach S3, so an entity's
  timeline is finding-derived. `/entities/:id/events` says so in its response.

## Tests

```bash
cd lambdas/api/tests && ../.venv/bin/python -m unittest discover -s .

# plus the checks that need AWS credentials
UEBA_LIVE_TESTS=1 AWS_PROFILE_OVERRIDE=snowbit-research \
  ../.venv/bin/python -m unittest discover -s .
```

73 tests: the weight/severity invariant, scoring calibration and the
repetition-saturation guard, chain sessionizing and the
cross-entity IP merge, the detector sink's day partitioning, and a live check
that every reason kind in S3 is in the catalog.

## Layout

| Path | |
|---|---|
| `ui/` | React SPA. `src/api.ts` is the whole client surface |
| `lambdas/api/serving/` | catalog, normalize, derive, build, routes, s3store, config |
| `lambdas/api/local_server.py` | dev server on :8787 |
| `lambdas/api/lambda_handler.py` | Function URL entrypoint |
| `lambdas/api/snapshot.py` | builder — Lambda handler and CLI |
| `lambdas/api/backfill.py` | one-time Coralogix → S3 seed (30 days, ~29k findings) |
| `lambdas/api/deploy.sh` | idempotent deploy |
| `lambdas/detector_patch/` | the six-line detector change, and its rollback notes |
