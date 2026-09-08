"""Who the console serves, and where each customer's state lives.

One deployment answers for every customer. A tenant is resolved per request
from `?tenant=` and threaded explicitly from there — into `s3store` as the
bucket, and into `build` as the prefix everything is written under. Nothing
here is read from a module global at import time, which is the property that
makes cross-tenant leakage a `TypeError` rather than a silent wrong answer.

## Why the registry is compiled in

`deploy.sh` has to know every (bucket, prefix) pair *before* the IAM policy
exists, to write that policy — so the registry cannot itself live in S3. It
imports this module directly:

    python3 -c 'from serving.tenants import all; ...'

which is why nothing here imports `boto3` or reads AWS. `UEBA_TENANTS` is the
escape hatch for a deployment that needs to add or repoint a tenant without a
code change; it is merged over these defaults by id.

## Adding a customer

Add an entry below with the detector's own `STATE_BUCKET` / `STATE_PREFIX` —
they must match exactly, since the detector writes the baseline and findings
this console reads. Then re-run `deploy.sh`, which will widen the IAM policy,
add the bucket notification, and add the lifecycle rule for the new prefix.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os

log = logging.getLogger(__name__)

# Kept in step with the detector's own sharding. Imported from `config` at use
# time rather than here, to keep this module free of import cycles.
_DEFAULT_REGION = "ap-south-1"


class UnknownTenant(KeyError):
    """Asked for a tenant that is not in the registry."""


@dataclasses.dataclass(frozen=True)
class Tenant:
    """One customer, and every location that belongs to them.

    Frozen because a request handler must never be able to repoint the tenant
    it was given partway through serving a response.
    """

    id: str
    label: str
    # The identity provider the findings come from, for the console's own
    # labels. Not used to branch on behaviour — the record's `detection_kind`
    # and reason kinds decide that, so a tenant running both would still work.
    product: str
    # Fallback for a timeline row that names no application. This is the only
    # place the old hardcoded "Azure AD SignInLogs" string survives.
    source_label: str
    bucket: str
    state_prefix: str
    region: str = _DEFAULT_REGION

    # Coralogix deep-link configuration. These travel with the bucket because
    # they describe the same deployment: the cluster the detector ships to, and
    # the application/subsystem pair its findings and its source logs carry.
    coralogix_ui_base: str = ""
    coralogix_anomaly_app: str = ""
    coralogix_anomaly_subsystem: str = ""
    coralogix_source_app: str = ""
    coralogix_source_subsystem: str = ""
    coralogix_secret_arn: str = ""

    # --- derived locations ------------------------------------------------
    # These reproduce, exactly, the strings `config.py` used to compute at
    # import time. `tests/test_tenants.py` pins them against literals, because
    # a one-character drift here silently repoints a whole customer.

    @property
    def baseline_key(self) -> str:
        """The detector owns this. The serving layer only ever reads it."""
        return f"{self.state_prefix}/baseline/baseline.json"

    @property
    def detector_state_key(self) -> str:
        return f"{self.state_prefix}/baseline/detector_state.json"

    @property
    def ui_prefix(self) -> str:
        """Everything the serving layer owns, so nothing it writes can collide
        with detector state."""
        return f"{self.state_prefix}/ui"

    @property
    def anomaly_prefix(self) -> str:
        return f"{self.ui_prefix}/anomalies"

    @property
    def snapshot_prefix(self) -> str:
        return f"{self.ui_prefix}/snapshot/v1"

    @property
    def feed_key(self) -> str:
        return f"{self.snapshot_prefix}/feed.json.gz"

    def snapshot_key(self, name: str) -> str:
        return f"{self.snapshot_prefix}/{name}"

    def entity_shard_key(self, entity: str, shards: int | None = None) -> str:
        from . import config

        return (f"{self.snapshot_prefix}/entity/"
                f"shard-{config.entity_shard(entity, shards)}.json.gz")

    def owns(self, bucket: str, key: str) -> bool:
        """Whether an S3 object belongs to this tenant.

        Both halves, deliberately. Bucket alone breaks the moment two tenants
        share one; prefix alone breaks across buckets, which is the arrangement
        we actually have.
        """
        return bucket == self.bucket and key.startswith(self.state_prefix + "/")

    def public(self) -> dict:
        """What `/api/tenants` may say about this tenant.

        A display contract, not the registry: the bucket, the prefix and the
        secret ARN are infrastructure and never leave the process.
        """
        return {"id": self.id, "label": self.label, "product": self.product,
                "source": self.source_label}


# Registry order is the order of the console's dropdown.
_DEFAULTS: tuple[Tenant, ...] = (
    Tenant(
        id="jiostar",
        label="JioStar",
        product="Microsoft Entra ID",
        source_label="Azure AD SignInLogs",
        bucket="azuread-anomaly-iforest-state-119418761367-ap2",
        state_prefix="tenants/jiostar",
        coralogix_anomaly_app="AzureAD_Anomaly_Detection",
        coralogix_anomaly_subsystem="iforest-ap2-v3",
        coralogix_source_app="Azure",
        coralogix_source_subsystem="AzureAD",
        coralogix_secret_arn=(
            "arn:aws:secretsmanager:ap-south-1:119418761367:secret:"
            "coralogix/azuread-anomaly/ap2-olIsZ4"
        ),
    ),
    Tenant(
        id="deel",
        label="Deel",
        product="Okta",
        source_label="Okta System Log",
        bucket="okta-anomaly-iforest-state-119418761367-eu1",
        state_prefix="tenants/deel",
        coralogix_anomaly_app="Okta_Anomaly_Detection",
        coralogix_anomaly_subsystem="iforest-eu1-deel",
        coralogix_source_app="okta",
        coralogix_source_subsystem="Okta-audit",
        coralogix_secret_arn=(
            "arn:aws:secretsmanager:ap-south-1:119418761367:secret:"
            "coralogix/okta-anomaly/eu1-deel-0PErju"
        ),
    ),
)

_FIELDS = {f.name for f in dataclasses.fields(Tenant)}


def _merge_env(base: tuple[Tenant, ...]) -> tuple[Tenant, ...]:
    """Apply `UEBA_TENANTS`, a JSON array of partial tenants keyed by `id`.

    An entry naming an existing id patches it; an entry naming a new one is
    appended. A malformed value is logged and ignored rather than raising —
    a bad environment variable must not take the whole API down, and the
    compiled-in defaults are always serviceable.
    """
    raw = os.environ.get("UEBA_TENANTS")
    if not raw:
        return base
    try:
        patches = json.loads(raw)
        if not isinstance(patches, list):
            raise ValueError("expected a JSON array")
    except (ValueError, TypeError) as e:
        log.warning("ignoring UEBA_TENANTS: %s", e)
        return base

    out = list(base)
    for patch in patches:
        if not isinstance(patch, dict) or not patch.get("id"):
            log.warning("ignoring UEBA_TENANTS entry without an id: %r", patch)
            continue
        unknown = set(patch) - _FIELDS
        if unknown:
            log.warning("ignoring unknown UEBA_TENANTS field(s) %s on %r",
                        sorted(unknown), patch["id"])
        fields = {k: v for k, v in patch.items() if k in _FIELDS}
        for i, t in enumerate(out):
            if t.id == fields["id"]:
                out[i] = dataclasses.replace(t, **{k: v for k, v in fields.items()
                                                   if k != "id"})
                break
        else:
            try:
                out.append(Tenant(**fields))
            except TypeError as e:
                log.warning("ignoring incomplete UEBA_TENANTS entry %r: %s",
                            fields["id"], e)
    return tuple(out)


_REGISTRY: tuple[Tenant, ...] = _merge_env(_DEFAULTS)
_BY_ID: dict[str, Tenant] = {t.id: t for t in _REGISTRY}

# Which tenant answers a request that names none. Every pre-existing caller —
# the ops `curl`, `deploy.sh`'s smoke test, a cached older UI bundle — was
# already getting this one, so they cannot be surprised by it.
DEFAULT_TENANT_ID = os.environ.get("DEFAULT_TENANT_ID") or _REGISTRY[0].id


def all() -> list[Tenant]:  # noqa: A001 - reads as `tenants.all()` at the call site
    return list(_REGISTRY)


def ids() -> list[str]:
    return [t.id for t in _REGISTRY]


def get(tenant_id: str) -> Tenant:
    try:
        return _BY_ID[tenant_id]
    except KeyError:
        raise UnknownTenant(tenant_id) from None


def default() -> Tenant:
    return _BY_ID.get(DEFAULT_TENANT_ID, _REGISTRY[0])


def resolve_s3(bucket: str, key: str) -> Tenant | None:
    """Which tenant an S3 event is about. None when no tenant claims it.

    None, never the default: a snapshot rebuild triggered by one customer's
    baseline must never write another customer's snapshot, so an unclaimed
    event is dropped and logged rather than falling through.
    """
    for t in _REGISTRY:
        if t.owns(bucket, key):
            return t
    return None
