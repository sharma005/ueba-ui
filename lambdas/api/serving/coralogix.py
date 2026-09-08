"""Coralogix DataPrime query client.

Lifted verbatim from the detector's `src/coralogix.py` so the backfill reads the
anomaly stream with exactly the semantics that wrote it — same NDJSON handling,
same 429 backoff. The Singles ingest half is not copied: the serving layer only
ever reads.
"""


from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterable, Iterator

import requests


log = logging.getLogger(__name__)


class DataPrimeClient:
    """Streaming NDJSON DataPrime query client.

    `coralogix.in` returns one JSON object per line: queryId, result(s),
    warnings, statistics. We yield only the row userData dicts.
    """

    def __init__(self, url: str, api_key: str, timeout: int = 600):
        self.url = url
        self.api_key = api_key
        self.timeout = timeout

    def query(
        self,
        dataprime_query: str,
        start_iso: str,
        end_iso: str,
        tier: str = "TIER_ARCHIVE",
        default_limit: int = 50000,
        max_retries: int = 5,
    ) -> Iterator[dict[str, Any]]:
        """Query DataPrime with short 429 backoff.

        Cap total sleep so Lambda can raise, checkpoint a partial baseline, and
        upload to S3 before the 900s hard timeout (long sleeps were killing the
        process mid-backoff with zero persistence).
        """
        payload = {
            "query": dataprime_query,
            "metadata": {
                "tier": tier,
                "syntax": "QUERY_SYNTAX_DATAPRIME",
                "startDate": start_iso,
                "endDate": end_iso,
                "defaultSource": "logs",
                "limit": default_limit,
            },
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_exc: Exception | None = None
        # ~3+6+12+15+15 ≈ 51s worst-case sleep per query — leave headroom to checkpoint.
        for attempt in range(max_retries):
            try:
                with requests.post(
                    self.url,
                    headers=headers,
                    data=json.dumps(payload),
                    timeout=self.timeout,
                    stream=True,
                ) as resp:
                    if resp.status_code == 429:
                        ra = resp.headers.get("Retry-After")
                        if ra and ra.isdigit():
                            wait = min(15, int(ra))
                        else:
                            wait = min(15, 3 * (2 ** attempt))
                        log.warning(
                            "dataprime 429 on attempt %d/%d — sleeping %ss",
                            attempt + 1, max_retries, wait,
                        )
                        time.sleep(wait)
                        continue
                    resp.raise_for_status()
                    for raw in resp.iter_lines(decode_unicode=True):
                        if not raw:
                            continue
                        try:
                            obj = json.loads(raw)
                        except json.JSONDecodeError:
                            log.warning("non-json line from dataprime: %s", raw[:200])
                            continue
                        if "result" in obj:
                            for r in obj["result"].get("results", []):
                                ud = r.get("userData")
                                if isinstance(ud, str):
                                    try:
                                        yield json.loads(ud)
                                    except json.JSONDecodeError:
                                        continue
                                elif isinstance(ud, dict):
                                    yield ud
                        elif "warning" in obj:
                            log.warning("dataprime warning: %s", obj["warning"])
                        elif "error" in obj:
                            raise RuntimeError(f"dataprime error: {obj['error']}")
                    return
            except requests.RequestException as e:
                last_exc = e
                wait = min(15, 3 * (2 ** attempt))
                log.warning(
                    "dataprime request error on attempt %d/%d: %s — sleeping %ss",
                    attempt + 1, max_retries, e, wait,
                )
                time.sleep(wait)
        if last_exc:
            raise last_exc
        raise RuntimeError("dataprime query failed after retries (likely 429)")
