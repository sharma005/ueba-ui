"""Deployment-wide knobs, and the one rule the builder and the API must share.

What is *not* here any more: the bucket, the tenant id, the state prefix and
every key derived from them. Those are per-customer and live in `tenants.py`,
resolved per request and passed explicitly. They used to be module constants
read at import time, which meant one deployment could only ever answer for one
customer — and, worse, that any code path forgetting to say which customer it
meant silently got JioStar's.

Everything below is genuinely deployment-wide: it is the same for every tenant
this process serves.
"""

from __future__ import annotations

import os

REGION = os.environ.get("AWS_REGION") or os.environ.get("STATE_REGION", "ap-south-1")

# Per-entity documents are sharded rather than written one object per entity:
# 6.5k accounts carry an anomaly in a 30-day window, and one PUT each every two
# hours is 79k PUTs a day for no benefit. The target is a shard around 1 MB, so
# serving any entity page is a single small GET.
#
# 256, not 64: each page now carries the account's whole baseline record rather
# than a top-10 of each counter, which measured at ~20 KB per profile against
# the live baseline. At 64 shards that is ~3 MB parsed per shard; at 256 it is
# back under 1 MB. 256 is also the ceiling — `entity_shard` keys off a single
# sha1 byte, so anything larger would leave shards permanently empty.
ENTITY_SHARDS = 256

# The feed `/anomalies` is served from. The UI always asks with `limit` (1000 at
# most), and 30 days of findings is ~30k records / ~45 MB — far too much to load
# per request, so the snapshot carries the most recent slice and reports the
# true total alongside it.
FEED_SIZE = 2500

# Set to require `X-Api-Key` on every request. Unset means auth is off, and
# `/health` reports `api_key_required: false` so the UI can say so rather than
# implying a protection that is not there.
API_KEY = os.environ.get("UI_API_KEY") or ""

# Browser origins allowed to call the Function URL.
ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get(
        "ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",") if o.strip()
]


def entity_shard(entity: str, shards: int | None = None) -> str:
    """Which shard holds an entity's page document.

    Builder and API must agree exactly, so the rule lives here rather than in
    either of them: sha1 of the lowercased key, first byte modulo the shard
    count. sha1 (not `hash()`) because Python salts `hash()` per process.

    Tenant-independent on purpose — the shard is a function of the entity name
    alone, and the tenant is already expressed by the prefix the key is written
    under (`Tenant.entity_shard_key`).

    `shards` exists because the two sides do not deploy together. A reader on
    256 against a snapshot a 64-shard builder wrote resolves byte 0x79 to
    `shard-79`, an object that build never rewrites — so the page was served
    from whatever stale copy an earlier 256-shard run had left behind, and an
    account's findings silently stopped at that run's clock while the feed on
    the same screen kept moving. Readers pass the count `meta.json` records for
    the snapshot in hand; only the builder uses the compiled-in default.
    """
    import hashlib

    n = shards or ENTITY_SHARDS
    h = hashlib.sha1(entity.strip().lower().encode()).digest()[0]
    return f"{h % n:02x}"
