"""Everything the detector does not compute: entity risk and attack chains.

The detector scores one *event* at a time and then forgets it. A UEBA console
asks different questions — who is risky right now, which findings are one
story — and both are derived here from the normalized anomaly stream.

Nothing in this module reads S3 or Coralogix; it is pure functions over lists of
anomalies, so `tests/test_derive.py` exercises it directly.
"""

from __future__ import annotations

import collections
import hashlib
import math
from typing import Iterable

from . import catalog

DAY_MS = 86_400_000

# --- Scoring ---------------------------------------------------------------
#
# HALF_LIFE_DAYS  a finding's contribution halves every 7 days, so a quiet
#                 account decays out of the rankings on its own.
# WINDOW_DAYS     matches the detector's own 30-day baseline lookback: scoring
#                 over a window the baseline does not cover would compare
#                 behaviour against a profile that never saw it.
# HALF_SATURATION the combined points at which an entity scores 50. Chosen so the
#                 bands below are reachable by *distinct* findings: one fresh
#                 critical (30 pts) scores ~49 = medium, two of different kinds
#                 ~74 = high, four ~93 = notable. A plain capped sum instead pins
#                 every busy account at exactly 100 and destroys the ranking.
HALF_LIFE_DAYS = 7.0
WINDOW_DAYS = 30
HALF_SATURATION = 30.0

# --- Why repetition of one detection must not accumulate ---------------------
#
# The detector emits one incident per account per UTC day, so an account that
# does the same benign thing daily produces one finding a day, every day. Summed
# linearly over 30 days that reaches any threshold: measured on real jiostar
# data, `legal.clms@jiostar.com` scored a maximum 100 from 34 `legacy_auth`
# findings and nothing else — a service account doing IMAP on schedule, ranked
# level with an account under an actual credential attack.
#
# So points saturate *per detection kind* before being combined. A kind can
# contribute at most KIND_CEILING, which is deliberately below the notable band:
# no single repeated signal, however often it fires, can make an account
# notable on its own. Breadth across kinds is what escalates, which is also how
# the detector itself reasons (noisy-OR across features, and it refuses to
# double-count its own location cluster).
#
# KIND_HALF is roughly one error-severity finding, so the first occurrence of a
# kind carries most of that kind's weight and the thirtieth carries almost none.
KIND_HALF = 20.0
KIND_CEILING = 45.0

# Cutoffs from theme.ts `levelOf`, so an API band and the colour the UI paints
# from the same score can never disagree. `/api/tuning` serves these, and the
# shell reads `bands.notable` as its critical threshold.
BANDS = {"notable": 90, "high": 70, "medium": 45}

# At or above this a chain is treated as alerting: the Chains page defaults to
# "alerting only" and the nav badge counts exactly these.
CHAIN_ALERT_SCORE = 70

# A chain breaks when an entity goes quiet for this long.
CHAIN_GAP_MS = DAY_MS

# Quality bar for a chain, on top of "two or more distinct tactics".
#
# Two tactics alone is far too weak on real data: `new_app` (Discovery) beside
# `unusual_hour` (Initial Access) is the single most common pairing in this
# tenant and is almost always someone opening an unfamiliar app late — 1,652 of
# 1,728 candidate chains were that shape. So a chain must also either contain a
# finding the *detector* rated error or critical, or cross a third tactic.
# Measured effect: 1,728 chains -> ~200, and the ones that survive read as
# stories (failed_signin_burst x4 -> credential_attack_volume -> legacy_auth).
CHAIN_MIN_SERIOUS_WEIGHT = 18
CHAIN_MIN_TACTICS_WITHOUT_SERIOUS = 3


# Bands ordered by urgency, for taking the stronger of two.
_BAND_ORDER = {"low": 0, "medium": 1, "high": 2, "notable": 3}


def band_of(score: float) -> str:
    if score >= BANDS["notable"]:
        return "notable"
    if score >= BANDS["high"]:
        return "high"
    if score >= BANDS["medium"]:
        return "medium"
    return "low"


def decayed_points_by_kind(anomalies: Iterable[dict], as_of_ms: int) -> dict[str, float]:
    """Decayed weight per detection kind, halving every `HALF_LIFE_DAYS`.

    Anomalies after `as_of_ms` are ignored, which is what lets the same function
    produce a score history and a 24h delta.
    """
    per_kind: dict[str, float] = collections.defaultdict(float)
    for a in anomalies:
        ts = a.get("ts") or 0
        if not ts or ts > as_of_ms:
            continue
        age_days = (as_of_ms - ts) / DAY_MS
        if age_days > WINDOW_DAYS:
            continue
        kind = a.get("detection_id") or "unknown"
        per_kind[kind] += float(a.get("weight") or 0) * (0.5 ** (age_days / HALF_LIFE_DAYS))
    return dict(per_kind)


def decayed_points(anomalies: Iterable[dict], as_of_ms: int) -> float:
    """Raw decayed weight, with no per-kind saturation. Diagnostics only."""
    return sum(decayed_points_by_kind(anomalies, as_of_ms).values())


def combined_points(anomalies: Iterable[dict], as_of_ms: int) -> float:
    """Decayed points after each kind saturates. See the KIND_* note above."""
    return sum(
        KIND_CEILING * (1.0 - 0.5 ** (pts / KIND_HALF))
        for pts in decayed_points_by_kind(anomalies, as_of_ms).values()
        if pts > 0
    )


def score_at(anomalies: Iterable[dict], as_of_ms: int) -> int:
    """Combined points mapped onto 0-100 by a saturating curve."""
    pts = combined_points(anomalies, as_of_ms)
    if pts <= 0:
        return 0
    return int(round(100.0 * (1.0 - 0.5 ** (pts / HALF_SATURATION))))


def top_detections(anomalies: Iterable[dict], limit: int = 5) -> list[dict]:
    """`TopDetection[]` — which detections drive this entity's score."""
    points: dict[str, float] = collections.defaultdict(float)
    counts: collections.Counter = collections.Counter()
    for a in anomalies:
        did = a.get("detection_id") or "unknown"
        points[did] += float(a.get("weight") or 0)
        counts[did] += 1
    rows = [
        {
            "detection_id": did,
            "name": catalog.label(did),
            "points": int(round(pts)),
            "count": counts[did],
        }
        for did, pts in points.items()
    ]
    rows.sort(key=lambda r: (-r["points"], -r["count"], r["detection_id"]))
    return rows[:limit]


def history(anomalies: list[dict], now_ms: int, days: int) -> list[dict]:
    """`{dt, score}` per day, oldest first, ending today."""
    out = []
    for i in range(days - 1, -1, -1):
        at = now_ms - i * DAY_MS
        dt = _day_str(at)
        out.append({"dt": dt, "score": score_at(anomalies, at)})
    return out


def _day_str(ms: int) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc).strftime("%Y-%m-%d")


# --- Entities --------------------------------------------------------------


def entity_rows(by_entity: dict[str, list[dict]], now_ms: int) -> dict[str, dict]:
    """Per-entity scoring facts, keyed by entity.

    Computed once and reused by the index, the notables list and every entity
    page, so a score can never differ between two screens.
    """
    rows: dict[str, dict] = {}
    for entity, anoms in by_entity.items():
        score = score_at(anoms, now_ms)
        prev = score_at(anoms, now_ms - DAY_MS)
        last_ts = max((a.get("ts") or 0) for a in anoms) if anoms else 0
        rows[entity] = {
            "entity": entity,
            "entity_type": "user",
            "score": score,
            "band": band_of(score),
            "delta_24h": score - prev,
            "last_anomaly_ts": last_ts,
            "n_anomalies_24h": sum(1 for a in anoms if (a.get("ts") or 0) >= now_ms - DAY_MS),
            "n_anomalies": len(anoms),
        }
    return rows


# --- Chains ----------------------------------------------------------------


def _step(a: dict) -> dict:
    det = catalog.get(a.get("detection_id"))
    return {
        "anomaly_id": a["anomaly_id"],
        "ts": a["ts"],
        "entity": a.get("entity") or "",
        "entity_type": "user",
        "detection_id": a.get("detection_id"),
        # Not `a["name"]`: a step can be built from an anomaly normalized by an
        # older snapshot, which carried the curated prose there.
        "name": catalog.label(a.get("detection_id")),
        "tactic": det.tactic,
        "technique": det.technique,
        "weight": a.get("weight") or 0,
        # Every finding here is one this engine produced. "platform" is reserved
        # for projected Coralogix signals, which this path does not read.
        "origin": "native",
    }


def chains(by_entity: dict[str, list[dict]], now_ms: int,
           days: int = WINDOW_DAYS) -> list[dict]:
    """Findings that tell one story, grouped into chains.

    Built in two passes:

      1. **Per entity** — sessionize that account's findings on a 24h gap, then
         keep a session only if it clears `_worth_chaining`: two distinct MITRE
         tactics, plus either a serious finding or a third tactic. One account
         failing `unusual_hour` nine times is not a chain; the same account
         going credential-attack -> sign-in -> session-reuse is.
      2. **Across entities** — merge sessions that overlap in time and share a
         source address. This is the only way a spray shows up as one incident
         instead of forty unrelated accounts, and it is why
         `spray_infrastructure` findings (which have no entity of their own)
         still earn their place.
    """
    floor = now_ms - days * DAY_MS
    sessions: list[dict] = []

    for entity, anoms in by_entity.items():
        window = sorted((a for a in anoms if (a.get("ts") or 0) >= floor),
                        key=lambda a: a["ts"])
        if len(window) < 2:
            continue
        group: list[dict] = []
        for a in window:
            if group and a["ts"] - group[-1]["ts"] > CHAIN_GAP_MS:
                sessions.append(_session(entity, group))
                group = []
            group.append(a)
        if group:
            sessions.append(_session(entity, group))

    sessions = [s for s in sessions if _worth_chaining(s)]
    merged = _merge_on_shared_ip(sessions)

    out = [_chain_doc(s) for s in merged]
    out.sort(key=lambda c: -c["combined_score"])
    return out


def _worth_chaining(s: dict) -> bool:
    """Whether a session is a story rather than a coincidence. See CHAIN_* above."""
    if len(s["anomalies"]) < 2 or len(s["tactics"]) < 2:
        return False
    if len(s["tactics"]) >= CHAIN_MIN_TACTICS_WITHOUT_SERIOUS:
        return True
    return any(float(a.get("weight") or 0) >= CHAIN_MIN_SERIOUS_WEIGHT
               for a in s["anomalies"])


def _session(entity: str, anoms: list[dict]) -> dict:
    tactics = []
    for a in anoms:
        t = catalog.get(a.get("detection_id")).tactic
        if t not in tactics:
            tactics.append(t)
    ips = {
        (a.get("evidence") or {}).get("ip")
        for a in anoms
        if (a.get("evidence") or {}).get("ip")
    }
    return {
        "entities": [entity],
        "anomalies": list(anoms),
        "tactics": tactics,
        "ips": ips,
        "first_ts": anoms[0]["ts"],
        "last_ts": anoms[-1]["ts"],
    }


def _merge_on_shared_ip(sessions: list[dict]) -> list[dict]:
    """Union-find over sessions that share an address and overlap in time."""
    parent = list(range(len(sessions)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    by_ip: dict[str, list[int]] = collections.defaultdict(list)
    for i, s in enumerate(sessions):
        for ip in s["ips"]:
            by_ip[ip].append(i)

    for idxs in by_ip.values():
        if len(idxs) < 2:
            continue
        for a_i, b_i in zip(idxs, idxs[1:]):
            a, b = sessions[a_i], sessions[b_i]
            # Overlap, or near enough that one attacker session explains both.
            if a["first_ts"] - CHAIN_GAP_MS <= b["last_ts"] and b["first_ts"] - CHAIN_GAP_MS <= a["last_ts"]:
                union(a_i, b_i)

    groups: dict[int, list[int]] = collections.defaultdict(list)
    for i in range(len(sessions)):
        groups[find(i)].append(i)

    out: list[dict] = []
    for members in groups.values():
        if len(members) == 1:
            out.append(sessions[members[0]])
            continue
        anoms: list[dict] = []
        entities: list[str] = []
        ips: set = set()
        for i in members:
            s = sessions[i]
            anoms.extend(s["anomalies"])
            ips |= s["ips"]
            for e in s["entities"]:
                if e not in entities:
                    entities.append(e)
        anoms.sort(key=lambda a: a["ts"])
        # Dedupe: one anomaly can only appear once in a chain.
        seen: set = set()
        uniq = []
        for a in anoms:
            if a["anomaly_id"] in seen:
                continue
            seen.add(a["anomaly_id"])
            uniq.append(a)
        tactics: list[str] = []
        for a in uniq:
            t = catalog.get(a.get("detection_id")).tactic
            if t not in tactics:
                tactics.append(t)
        out.append({
            "entities": entities,
            "anomalies": uniq,
            "tactics": tactics,
            "ips": ips,
            "first_ts": uniq[0]["ts"],
            "last_ts": uniq[-1]["ts"],
        })
    return out


def _chain_doc(s: dict) -> dict:
    anoms = s["anomalies"]
    steps = [_step(a) for a in anoms]
    # Same per-kind saturation as an entity score, and for the same reason: a
    # chain carrying eight repeats of one signal is not eight times the story.
    # Scored as of its own last step, so an old chain is not decayed to nothing
    # while still being displayed as an incident that happened.
    pts = combined_points(anoms, s["last_ts"])
    # A chain is worth more than its parts: crossing tactics is the signal.
    diversity = 1.0 + 0.15 * (len(s["tactics"]) - 1)
    score = int(round(100.0 * (1.0 - 0.5 ** (pts * diversity / HALF_SATURATION))))
    return {
        "chain_id": _stable_id("chain", ",".join(sorted(s["entities"])), str(s["first_ts"])),
        "first_ts": s["first_ts"],
        "last_ts": s["last_ts"],
        # Type-prefixed, as the UI's chain graph expects.
        "entities": [f"user:{e}" for e in s["entities"]],
        "entity_count": len(s["entities"]),
        "anomaly_ids": [a["anomaly_id"] for a in anoms],
        "n_anomalies": len(anoms),
        "tactic_sequence": s["tactics"],
        "detection_mix": dict(collections.Counter(a.get("detection_id") for a in anoms)),
        "origin_mix": {"native": len(anoms)},
        "combined_score": score,
        "band": band_of(score),
        "steps": steps,
    }


def _stable_id(*parts: str) -> str:
    """Deterministic id, so a rebuild does not renumber everything.

    The UI keys rows and open/closed panels by these; a random id would reset
    the analyst's expanded rows on every 2-hourly rebuild.
    """
    h = hashlib.sha1("|".join(parts).encode()).hexdigest()
    return f"{parts[0]}-{h[:16]}"


__all__ = [
    "BANDS", "CHAIN_ALERT_SCORE", "HALF_LIFE_DAYS", "HALF_SATURATION",
    "KIND_CEILING", "KIND_HALF", "WINDOW_DAYS",
    "band_of", "chains", "combined_points", "decayed_points",
    "decayed_points_by_kind", "entity_rows", "history", "score_at",
    "top_detections",
]
