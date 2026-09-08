"""Deep links from a finding back to the log that produced it.

The detector ships every finding to Coralogix and records `incident_id`,
`event_id` and `sample_event_ids`, but no URL — nothing in the log stream says
where to go and look at it. This module builds that link.

## Why it is configured rather than derived

A Coralogix UI link needs the *team* subdomain (`https://<team>.app.coralogix.in`).
That is not in the detector's config, not in the Coralogix secret (which holds
only the API endpoints `ng-api-http.app.coralogix.in` and the ingest host), and
not anywhere else in the AWS account. So it is supplied by
`CORALOGIX_UI_BASE`, and with it unset no link is emitted at all — a link to a
guessed host would take an analyst somewhere that either 404s or, worse, belongs
to a different tenant.

`CORALOGIX_LOG_URL_TEMPLATE` overrides the URL shape entirely, for when the
console's query-page parameters differ from the default below. It is formatted
with `{base}`, `{query}` (already URL-encoded), `{start}` and `{end}` (ISO 8601).

## Why this is per tenant

Each customer's detector ships to its own Coralogix cluster under its own
application/subsystem pair — `AzureAD_Anomaly_Detection` on ap2 for one,
`Okta_Anomaly_Detection` on eu1 for another. Those facts travel with the bucket
and the prefix because they describe the same deployment, so they live on
`Tenant` and are read through `for_tenant`. When these were module globals, a
link built for one customer pointed at another customer's log stream — which is
precisely the failure the `CORALOGIX_UI_BASE` note above exists to prevent.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
from urllib.parse import quote

from .tenants import Tenant

# Coralogix's logs query page. Overridable because these parameters have changed
# across UI versions and this cannot be verified from here. Deployment-wide, not
# per tenant: it describes the Coralogix UI version, not the customer.
DEFAULT_TEMPLATE = (
    "{base}/#/query-new/logs"
    "?query={query}&querySyntax=dataprime&time=from:{start},to:{end}"
)
TEMPLATE = os.environ.get("CORALOGIX_LOG_URL_TEMPLATE") or DEFAULT_TEMPLATE

# How far either side of the event to open the log window. The detector runs on a
# 2h cadence and an incident can group events across a run, so a tight window
# would sometimes land outside the record it is meant to show.
WINDOW_HOURS = 6


def _env(name: str, tenant_id: str, fallback: str) -> str:
    """A per-tenant environment override, falling back to the shared name.

    `CORALOGIX_UI_BASE_DEEL` beats `CORALOGIX_UI_BASE` beats the registry, so a
    single-tenant deployment's existing unsuffixed variables keep working
    unchanged.
    """
    suffix = tenant_id.upper().replace("-", "_")
    return (os.environ.get(f"{name}_{suffix}")
            or os.environ.get(name)
            or fallback)


@dataclasses.dataclass(frozen=True)
class LinkConfig:
    """One tenant's Coralogix coordinates. Build with `for_tenant`."""

    ui_base: str
    anomaly_app: str
    anomaly_subsystem: str
    source_app: str
    source_subsystem: str
    template: str = TEMPLATE

    def configured(self) -> bool:
        """Whether links can be built at all. Drives `/health`'s report."""
        return bool(self.ui_base)

    def _url(self, query: str, ts_ms: int) -> str:
        span = WINDOW_HOURS * 3_600_000
        return self.template.format(
            base=self.ui_base,
            query=quote(query, safe=""),
            start=_iso(ts_ms - span),
            end=_iso(ts_ms + span),
        )

    def finding_url(self, incident_id: str | None, ts_ms: int) -> str | None:
        """The detector's own finding record — the log this row was built from."""
        if not self.ui_base or not incident_id or not ts_ms:
            return None
        q = (
            f"source logs | filter $l.applicationname == '{self.anomaly_app}'"
            f" && $l.subsystemname == '{self.anomaly_subsystem}'"
            f" && incident_id == '{_quote_literal(incident_id)}'"
        )
        return self._url(q, ts_ms)

    def signin_url(self, event_ids: list[str] | None, ts_ms: int) -> str | None:
        """The raw sign-ins behind the finding.

        A finding groups up to ten sample events, so this filters on the set
        rather than one id — the point of the link is to see them as a group.
        """
        if not self.ui_base or not event_ids or not ts_ms:
            return None
        ids = ", ".join(f"'{_quote_literal(e)}'" for e in event_ids[:10] if e)
        if not ids:
            return None
        q = (
            f"source logs | filter $l.applicationname == '{self.source_app}'"
            f" && $l.subsystemname == '{self.source_subsystem}'"
            f" && properties.id:string in [{ids}]"
        )
        return self._url(q, ts_ms)


def for_tenant(t: Tenant) -> LinkConfig:
    return LinkConfig(
        # e.g. "https://myteam.app.coralogix.in" — no trailing slash needed.
        ui_base=_env("CORALOGIX_UI_BASE", t.id, t.coralogix_ui_base).rstrip("/"),
        anomaly_app=_env("CORALOGIX_ANOMALY_APP", t.id, t.coralogix_anomaly_app),
        anomaly_subsystem=_env("CORALOGIX_ANOMALY_SUBSYSTEM", t.id,
                               t.coralogix_anomaly_subsystem),
        source_app=_env("CORALOGIX_SOURCE_APP", t.id, t.coralogix_source_app),
        source_subsystem=_env("CORALOGIX_SOURCE_SUBSYSTEM", t.id,
                              t.coralogix_source_subsystem),
    )


def _iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def _quote_literal(value: str) -> str:
    """Escape a value for a DataPrime single-quoted string literal."""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")
