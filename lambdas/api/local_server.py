#!/usr/bin/env python3
"""Local dev server for the UI's `/api` prefix.

    lambdas/api/.venv/bin/python lambdas/api/local_server.py 8787

This is the address `ui/vite.config.ts` already proxies to, so with this running
`npm run dev` shows real data. It shares `serving.routes` with the Lambda, so a
response here is the response deployed — only the transport differs, and that
includes tenant resolution: `?tenant=` behaves here exactly as it does in
production.

Reads S3 with your own credentials; pass `--profile` (default
`snowbit-research`) or set `AWS_PROFILE_OVERRIDE`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

log = logging.getLogger("local_server")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # vite proxies same-origin, so CORS is not needed for the app itself —
        # it is here so `curl` and a browser opened straight at :8787 both work.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Api-Key")
        self.end_headers()
        self.wfile.write(body)

    def _run(self, method: str) -> None:
        from serving.routes import handle

        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        body = None
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "body is not JSON"})

        status, payload = handle(method, parsed.path, query, body, dict(self.headers))
        self._send(status, payload)

    def do_GET(self) -> None:
        self._run("GET")

    def do_POST(self) -> None:
        self._run("POST")

    def do_OPTIONS(self) -> None:
        self._send(204, {})

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s", fmt % args)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port", nargs="?", type=int, default=8787)
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE_OVERRIDE", "snowbit-research"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.profile:
        os.environ["AWS_PROFILE_OVERRIDE"] = args.profile

    from serving import config, tenants

    log.info("serving /api on http://127.0.0.1:%d (profile %s); default tenant %s",
             args.port, args.profile, tenants.default().id)
    for t in tenants.all():
        log.info("  %-10s s3://%s/%s", t.id, t.bucket, t.snapshot_prefix)
    if not config.API_KEY:
        log.info("UI_API_KEY unset — auth is off for local dev")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
