"""The detection catalog: what the detector's reason kinds mean to an analyst.

The detector emits `reasons[{kind, why, ...}]` and a severity label, but no
weight and no MITRE mapping — it was built to ship findings to Coralogix, not to
drive a console. This module is where that presentation layer lives, and it is
the single source of truth for `/overview`'s top-detections and MITRE panels and
for `/health`'s `detections_enabled` count.

## Detections are named by their raw reason kind, not by prose

Every display label comes from `label()`, which returns the `reasons[].kind`
string exactly as the logs carry it (`new_app`, `impossible_travel`). The
curated `Detection.name` strings below are deliberately **not** read by any wire
path: a console that invents its own vocabulary cannot be pivoted from into
Coralogix, and an analyst reading "First use of an application" has no way to
guess it should search for `new_app`.

They are kept because `bump`, `tactic`, `technique` and `blurb` still do real
work on the same rows, and `name` documents what each kind means for whoever
edits this file. `tests/test_catalog.py` asserts no `name` reaches the wire.

All 22 reason kinds the detector can emit are covered. Note that only 15 appear
as `"kind": "..."` literals in its `detect.py`; the other seven
(`new_country`, `rare_country`, `new_ip`, `new_device`, `rare_device`,
`new_app`, `rare_app`) are built by its `_rarity_reason` helper, which takes the
kind as an argument. `tests/test_catalog.py::test_every_observed_kind_is_known`
checks the live stream against this list so a kind added upstream shows up as a
failing test rather than as a mislabelled row.

## Why weight is derived from the detector's own severity

The UI colours an anomaly by running `weightToSev(weight)` (theme.ts):

    Critical >= 26   High >= 18   Medium >= 10   Low < 10

and the Anomaly Feed's severity filter is driven by nothing else. If weight were
an independent judgement, a finding the detector called `critical` could render
as Medium, and the filter would disagree with the pipeline about what is
critical. So weight is anchored to the detector's severity band and only ranked
*within* it by `bump` — the detector spent real effort on severity (suppression
rules, second-signal requirements, `cap_severity_when_azure_risk_none`), and
this keeps that judgement intact. `tests/test_catalog.py` asserts the invariant
holds for every kind in every band.
"""

from __future__ import annotations

# Band interiors from theme.ts `weightToSev`. Each is (base, low, high): weight
# is clamped into [low, high], so a bump can never cross a band boundary.
SEVERITY_BANDS: dict[str, tuple[int, int, int]] = {
    "critical": (30, 26, 40),
    "error": (21, 18, 25),
    "warning": (13, 10, 17),
    "info": (6, 1, 9),
    # Severities the detector can emit but rarely does; treated as context.
    "verbose": (3, 1, 9),
    "debug": (2, 1, 9),
}
DEFAULT_SEVERITY = "info"

# MITRE tactics, ranked for chain ordering. This is NOT ATT&CK's canonical
# enterprise order: in this detector's data the story runs credential attack ->
# successful sign-in -> evasion, so Credential Access is deliberately ranked
# ahead of Initial Access. `/correlations` serves these as `tactic_ranks` and the
# chain graph positions steps by them.
TACTIC_RANKS: dict[str, int] = {
    "Reconnaissance": 1,
    "Credential Access": 2,
    "Initial Access": 3,
    "Defense Evasion": 4,
    "Persistence": 5,
    "Privilege Escalation": 6,
    "Discovery": 7,
    "Lateral Movement": 8,
    "Collection": 9,
    "Exfiltration": 10,
    "Impact": 11,
}


class Detection:
    """One reason kind, as an analyst should see it.

    `bump` ranks this kind against others *inside* the severity band the
    detector assigned — it never moves a finding between bands.
    """

    __slots__ = ("kind", "name", "bump", "tactic", "technique", "blurb")

    def __init__(self, kind: str, name: str, bump: int, tactic: str,
                 technique: str, blurb: str):
        self.kind = kind
        self.name = name
        self.bump = bump
        self.tactic = tactic
        self.technique = technique
        self.blurb = blurb


def _d(*args) -> Detection:
    return Detection(*args)


# Ordered most to least actionable, which is also the tie-break when one finding
# carries several reasons: the earliest listed wins as the primary detection.
CATALOG: dict[str, Detection] = {
    d.kind: d
    for d in (
        _d("credential_attack_success", "Credential attack succeeded", 8,
           "Credential Access", "T1110 Brute Force",
           "A sign-in succeeded from a source that had been failing against this account."),
        _d("impossible_travel", "Impossible travel", 6,
           "Initial Access", "T1078.004 Valid Accounts: Cloud Accounts",
           "Two successful sign-ins too far apart to be the same person travelling."),
        _d("session_reuse_new_device", "Session token reused on a new device", 6,
           "Defense Evasion", "T1550.004 Use Alternate Authentication Material: Web Session Cookie",
           "One session id appeared on a different device and network — the hallmark of a stolen token."),
        _d("mfa_fatigue", "MFA fatigue / push bombing", 5,
           "Credential Access", "T1621 Multi-Factor Authentication Request Generation",
           "A burst of MFA challenges, then a success — consent won by exhaustion."),
        _d("concurrent_geolocations", "Concurrent sign-ins from separate countries", 4,
           "Initial Access", "T1078.004 Valid Accounts: Cloud Accounts",
           "Active sessions from countries that cannot both belong to one person at one time."),
        _d("new_country", "First sign-in from a new country", 5,
           "Initial Access", "T1078.004 Valid Accounts: Cloud Accounts",
           "A country never seen in this account's baseline. The detector treats it as "
           "soft-critical: strong, but it wants a second signal before paging."),
        _d("credential_attack_volume", "Credential attack volume", 4,
           "Credential Access", "T1110.003 Password Spraying",
           "Failed sign-ins against this account well past its own baseline."),
        _d("spray_infrastructure", "Spray infrastructure", 3,
           "Credential Access", "T1110.003 Password Spraying",
           "One address hitting many accounts — invisible to any per-account rule."),
        # No Azure AD counterpart — this one is genuinely new with Okta, which
        # labels the address itself. Ranked at 3 rather than higher because it
        # is usually accompanied by a login reason that describes what was
        # actually done, and that reason should still be able to win.
        _d("anonymizing_proxy", "Anonymizing proxy", 3,
           "Defense Evasion", "T1090 Proxy",
           "The sign-in arrived through Tor, a VPN or an anonymizing relay — the "
           "source address is deliberately obscured."),
        _d("azure_risk", "Microsoft Entra risk signal", 2,
           "Initial Access", "T1078.004 Valid Accounts: Cloud Accounts",
           "Microsoft's own risk detection fired on this sign-in."),
        # Adjacent to `azure_risk` and weighted identically: the same idea from
        # the other identity provider. The Okta detector even reads
        # `okta_risk_levels or azure_risk_levels` from its own config.
        _d("okta_risk", "Okta risk signal", 2,
           "Initial Access", "T1078.004 Valid Accounts: Cloud Accounts",
           "Okta's own risk detection fired on this sign-in."),
        _d("rare_country", "Sign-in from a rare country", 2,
           "Initial Access", "T1078.004 Valid Accounts: Cloud Accounts",
           "A country that is a fraction of a percent of this account's history."),
        _d("failed_signin_burst", "Failed sign-in burst", 1,
           "Credential Access", "T1110.001 Password Guessing",
           "A short burst of failures against one account."),
        _d("legacy_auth", "Legacy authentication protocol", 1,
           "Defense Evasion", "T1550 Use Alternate Authentication Material",
           "A protocol that predates modern MFA (IMAP/POP/SMTP and friends) was used."),
        _d("ca_failure", "Conditional Access failure", 0,
           "Defense Evasion", "T1556 Modify Authentication Process",
           "A Conditional Access policy did not pass on this attempt."),
        _d("new_ip", "First sign-in from a new address", 0,
           "Initial Access", "T1078.004 Valid Accounts: Cloud Accounts",
           "An address never seen for this account, on a network it does not normally use."),
        _d("new_device", "First sign-in from a new device", 0,
           "Initial Access", "T1078.004 Valid Accounts: Cloud Accounts",
           "A device fingerprint never seen for this account."),
        _d("unusual_hour", "Sign-in at an unusual hour", -1,
           "Initial Access", "T1078.004 Valid Accounts: Cloud Accounts",
           "Activity outside the hours this account normally keeps."),
        _d("rare_device", "Sign-in from a rare device", -1,
           "Initial Access", "T1078.004 Valid Accounts: Cloud Accounts",
           "A device that is a fraction of a percent of this account's history."),
        _d("new_app", "First use of an application", -2,
           "Discovery", "T1526 Cloud Service Discovery",
           "An application this account has never opened before. The most common finding "
           "by volume, and on its own almost always someone's ordinary first visit."),
        _d("rare_app", "Use of a rare application", -2,
           "Discovery", "T1526 Cloud Service Discovery",
           "An application that is a fraction of a percent of this account's history."),
        _d("new_ip_known_asn", "New address on a known network", -2,
           "Initial Access", "T1078.004 Valid Accounts: Cloud Accounts",
           "An address not seen before, but on a network this account already uses. "
           "The detector marks this informational."),
        _d("new_ip_mobile_isp", "New address on a mobile carrier", -3,
           "Initial Access", "T1078.004 Valid Accounts: Cloud Accounts",
           "A new carrier-NAT address. Mobile addresses rotate constantly, so this is "
           "context, not a finding."),
        _d("no_baseline", "No behavioural baseline yet", -3,
           "Initial Access", "T1078.004 Valid Accounts: Cloud Accounts",
           "Activity from an account with too little history to have been profiled."),
    )
}

# Ranking used to pick the primary reason of a multi-reason finding.
_PRIORITY = {kind: i for i, kind in enumerate(CATALOG)}

# Record-level `detection_kind` values, for findings whose reasons list is empty
# or carries a kind this catalog does not know.
DETECTION_KIND_FALLBACK: dict[str, str] = {
    # Deliberately absent: `azure_ad_credential_abuse_deviation`, the generic
    # per-account kind. It says nothing about *which* signal fired, so mapping
    # it to any specific detection would mislabel the finding. It falls through
    # to UNKNOWN ("Unclassified deviation") instead, which is visible in
    # `/overview`'s top detections the moment the detector grows a reason kind
    # this catalog has not learned yet.
    "azure_ad_credential_attack_volume": "credential_attack_volume",
    "azure_ad_credential_attack_success": "credential_attack_success",
    "azure_ad_mfa_fatigue": "mfa_fatigue",
    "azure_ad_spray_infrastructure": "spray_infrastructure",
    # Same four from the Okta detector. `okta_credential_abuse_deviation` is
    # absent for the same reason its Azure twin is.
    "okta_credential_attack_volume": "credential_attack_volume",
    "okta_credential_attack_success": "credential_attack_success",
    "okta_mfa_fatigue": "mfa_fatigue",
    "okta_spray_infrastructure": "spray_infrastructure",
}

# Telemetry the detector ships down the same stream. Never findings.
#
# Split by category because `build._pipeline_health` needs the two separately —
# one names the run, the other the baseline's condition. Membership, not a
# per-tenant config value: the record names its own kind, so a tenant emitting
# both vocabularies (an Okta migration mid-flight, say) is still read correctly.
RUN_SUMMARY_KINDS = frozenset({
    "azure_ad_credential_abuse_run_summary",
    "okta_credential_abuse_run_summary",
})
BASELINE_HEALTH_KINDS = frozenset({
    "azure_ad_baseline_health",
    "okta_baseline_health",
})
TELEMETRY_KINDS = RUN_SUMMARY_KINDS | BASELINE_HEALTH_KINDS

UNKNOWN = _d("unknown", "Unclassified deviation", 0, "Initial Access",
             "T1078.004 Valid Accounts: Cloud Accounts",
             "A finding whose reason kind is not in the catalog.")


def get(kind: str | None) -> Detection:
    return CATALOG.get(kind or "", UNKNOWN)


def label(kind: str | None) -> str:
    """The detection's name as the logs spell it.

    Verbatim on purpose: no prettifying, no `azure_ad_` stripping, no curated
    prose. The console names a detection with the same string an analyst would
    search for in Coralogix, so the two can never drift apart.

    This is the single place the display label is decided. `Detection.name` is
    still carried in the catalog but is no longer read by any wire path — see
    the module docstring.
    """
    return kind or "unknown"


def primary_kind(record: dict) -> str:
    """The one reason an analyst would name if asked what this finding is.

    Highest catalog priority wins, not first-in-list: the detector appends
    reasons in evaluation order, so `reasons[0]` is often the cheapest check
    (`unusual_hour`) rather than the reason the finding matters
    (`impossible_travel`).
    """
    kinds = [
        r.get("kind") for r in (record.get("reasons") or [])
        if isinstance(r, dict) and r.get("kind") in CATALOG
    ]
    if kinds:
        return min(kinds, key=lambda k: _PRIORITY[k])
    return DETECTION_KIND_FALLBACK.get(record.get("detection_kind") or "", "unknown")


def weight_for(kind: str, severity: str | None) -> int:
    """Points this finding contributes, inside the detector's severity band."""
    base, lo, hi = SEVERITY_BANDS.get(
        (severity or DEFAULT_SEVERITY).lower(), SEVERITY_BANDS[DEFAULT_SEVERITY]
    )
    return max(lo, min(hi, base + get(kind).bump))


def enabled_count() -> int:
    return len(CATALOG)
