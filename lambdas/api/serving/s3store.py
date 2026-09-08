"""S3 reads and writes, with the caching the request path needs.

The API is stateless per request but the snapshot it serves changes only every
two hours, so objects are cached in the module by ETag: a warm Lambda re-reads
nothing, and a rebuild is picked up on the next request without a deploy.

Every function takes the bucket explicitly. It used to read `config.BUCKET`,
which is what made the process single-tenant; passing it means a caller that
forgets which customer it is serving fails loudly at the call site instead of
quietly reading the default one. The cache is keyed by `(bucket, key)` for the
same reason — two tenants only have distinct keys today because their prefixes
happen to contain their ids, and that is a naming coincidence, not a guarantee
the registry enforces.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from . import config

log = logging.getLogger(__name__)

_client_lock = threading.Lock()
_clients: dict[str, Any] = {}
_cache: dict[tuple[str, str], tuple[str, Any]] = {}


def client(region: str | None = None):
    """One cached client per region.

    Per region rather than one overall because a tenant carries its own, and a
    bucket in another region would otherwise be reached through a client built
    for this one.
    """
    region = region or config.REGION
    got = _clients.get(region)
    if got is None:
        with _client_lock:
            got = _clients.get(region)
            if got is None:
                profile = os.environ.get("AWS_PROFILE_OVERRIDE")
                session = (
                    boto3.Session(profile_name=profile, region_name=region)
                    if profile else boto3.Session(region_name=region)
                )
                # The builder fans out ~64 parallel PUTs; the default pool of 10
                # makes botocore discard and rebuild connections under load.
                got = session.client(
                    "s3", config=BotoConfig(max_pool_connections=32)
                )
                _clients[region] = got
    return got


def _maybe_gunzip(body: bytes, key: str) -> bytes:
    if key.endswith(".gz") or body[:2] == b"\x1f\x8b":
        return gzip.decompress(body)
    return body


def get_json(bucket: str, key: str, default: Any = None) -> Any:
    """Read one JSON object. Missing keys return `default`, not an error.

    A missing snapshot is an ordinary state — it means the builder has not run
    yet — and the UI is written to render an empty result rather than an error,
    so this must not raise for a 404.
    """
    try:
        obj = client().get_object(Bucket=bucket, Key=key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NoSuchBucket"):
            return default
        raise
    return json.loads(_maybe_gunzip(obj["Body"].read(), key))


def get_json_cached(bucket: str, key: str, default: Any = None) -> Any:
    """As `get_json`, but skips the download when the ETag has not moved."""
    try:
        head = client().head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "403"):
            return default
        raise
    etag = head.get("ETag", "")
    slot = (bucket, key)
    hit = _cache.get(slot)
    if hit and hit[0] == etag:
        return hit[1]
    value = get_json(bucket, key, default)
    _cache[slot] = (etag, value)
    return value


def put_json(bucket: str, key: str, value: Any, gzip_it: bool = False) -> int:
    """Write one JSON object. Returns bytes written."""
    raw = json.dumps(value, separators=(",", ":"), default=str).encode()
    if gzip_it:
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
            gz.write(raw)
        raw = buf.getvalue()
    client().put_object(
        Bucket=bucket, Key=key, Body=raw,
        ContentType="application/json",
        **({"ContentEncoding": "gzip"} if gzip_it else {}),
    )
    return len(raw)


def list_keys(bucket: str, prefix: str) -> list[str]:
    keys: list[str] = []
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        page = client().list_objects_v2(**kw)
        keys.extend(c["Key"] for c in page.get("Contents", []))
        if not page.get("IsTruncated"):
            return keys
        token = page.get("NextContinuationToken")


def get_many_json(bucket: str, keys: list[str], workers: int = 16) -> list[Any]:
    """Parallel reads. The builder pulls ~30 day-files; serial GETs dominate it."""
    if not keys:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(keys))) as pool:
        return list(pool.map(lambda k: get_json(bucket, k, default=None), keys))


def put_many_json(bucket: str, items: list[tuple[str, Any]], workers: int = 16,
                  gzip_it: bool = False) -> int:
    if not items:
        return 0
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        return sum(pool.map(lambda kv: put_json(bucket, kv[0], kv[1], gzip_it=gzip_it), items))


def download(bucket: str, key: str, dest: str) -> str:
    """Stream a large object to disk. Used for the 150 MB baseline."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    client().download_file(bucket, key, dest)
    return dest


def clear_cache() -> None:
    _cache.clear()
