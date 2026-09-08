"""Lambda Function URL entrypoint for `/api/*`.

Thin by design: unpack the Function URL event, hand it to `serving.routes`, pack
the response back. All behaviour lives in `routes`, which the local server runs
unchanged, so there is no deployed-only code path to be surprised by.
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
    force=True,
)
log = logging.getLogger("lambda_handler")


def _cors(origin: str | None) -> dict:
    from serving import config

    allow = origin if origin and origin in config.ALLOWED_ORIGINS else (
        config.ALLOWED_ORIGINS[0] if config.ALLOWED_ORIGINS else "*"
    )
    return {
        "Access-Control-Allow-Origin": allow,
        "Access-Control-Allow-Headers": "Content-Type, X-Api-Key",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Vary": "Origin",
    }


def handler(event, context):
    from serving.routes import handle

    ctx = (event or {}).get("requestContext") or {}
    http = ctx.get("http") or {}
    method = (http.get("method") or "GET").upper()
    path = http.get("path") or event.get("rawPath") or "/"
    headers = {k.lower(): v for k, v in ((event or {}).get("headers") or {}).items()}
    query = (event or {}).get("queryStringParameters") or {}

    cors = _cors(headers.get("origin"))
    if method == "OPTIONS":
        return {"statusCode": 204, "headers": cors, "body": ""}

    body = None
    raw = (event or {}).get("body")
    if raw:
        if (event or {}).get("isBase64Encoded"):
            import base64

            raw = base64.b64decode(raw).decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            return {"statusCode": 400, "headers": cors,
                    "body": json.dumps({"error": "body is not JSON"})}

    status, payload = handle(method, path, query, body, headers)
    text = json.dumps(payload, default=str)
    resp_headers = {**cors, "Content-Type": "application/json",
                    # The snapshot only changes every 2h, but a stale console is
                    # worse than an extra request; let the browser revalidate.
                    "Cache-Control": "no-cache"}

    # The Function URL caps the response the runtime may post at ~6 MB and fails
    # the whole invocation with a 413 above it, so the anomaly feed used to 502
    # outright. Gzip puts a full feed an order of magnitude under the cap.
    #
    # Gated on the request rather than unconditional: the CloudFront cache policy
    # in front of this sets EnableAcceptEncodingGzip, so it normalises and
    # forwards `accept-encoding` and every browser gets the compressed path,
    # while a plain `curl` still gets readable JSON. `routes.anomalies` keeps its
    # own byte budget for the clients that land here without gzip.
    if "gzip" in (headers.get("accept-encoding") or "").lower() and len(text) > 1024:
        import base64
        import gzip

        return {
            "statusCode": status,
            "headers": {**resp_headers, "Content-Encoding": "gzip"},
            "body": base64.b64encode(gzip.compress(text.encode(), 6)).decode(),
            "isBase64Encoded": True,
        }

    return {"statusCode": status, "headers": resp_headers, "body": text}
