import type {
  Anomaly, AnomalyReason, EntityPage, IndexEntity, Investigation, NotableEntity,
  SignalCheck,
} from "../types";
import { bandToSev, weightToSev, type SeverityBands } from "../theme";
import {
  avatarColor, clockTime, initialsOf, relTime,
  type AnomalyVM, type EntityVM, type SignalRow,
} from "./models";

/**
 * Wire shapes -> view models.
 *
 * The rule this module exists to enforce: **nothing is invented here.** A field
 * the API did not send arrives as null and the screens render it as absent.
 * Earlier versions filled the gaps — a human name title-cased out of the email
 * local part, an entity "kind" guessed from an `svc-` prefix, a department
 * holding a peer-group id this pipeline never computes, a distinct-IP count
 * recovered by tallying addresses across the anomaly feed — and every one of
 * them read on screen exactly like a measurement.
 *
 * Severity is likewise never decided here: `weightToSev` and `bandToSev` take
 * the cutoffs `/api/tuning` served, so the adapters need `severityBands` passed
 * in rather than carrying a copy of the ladder.
 */

function baseEntity(entity: string, score: number, band: IndexEntity["band"]): EntityVM {
  return {
    id: entity,
    initials: initialsOf(entity),
    address: entity,
    band,
    score: Math.round(score),
    delta: 0,
    anomalies: null,
    last: null,
    color: avatarColor(entity),
    devices: null,
    ips: null,
  };
}

export function fromIndexEntity(e: IndexEntity): EntityVM {
  return {
    ...baseEntity(e.entity, e.score, e.band),
    last: relTime(e.last_anomaly_ts),
    delta: Math.round(e.delta_24h ?? 0),
    anomalies: e.n_anomalies ?? null,
  };
}

export function fromNotable(e: NotableEntity): EntityVM {
  return {
    ...baseEntity(e.entity, e.score, e.band),
    delta: Math.round(e.delta_24h),
    anomalies: e.n_anomalies_24h,
  };
}

/** Country/city/ASN the enrichment resolved for one source address. */
export interface IpGeo {
  country?: string | null;
  city?: string | null;
  asn?: string | null;
  lat?: number;
  lon?: number;
}

/**
 * The account's learned baseline, as the detector wrote it. Every name->count
 * map arrives untruncated, so a user with hundreds of addresses has hundreds of
 * entries here — panels scroll rather than slice.
 */
export interface Profile {
  days_seen?: number;
  first_seen_ms?: number;
  last_seen_ms?: number;
  login_count?: number;
  failed_login_count?: number;
  distinct_ips?: number;
  distinct_failed_ips?: number;
  user_name?: string | null;
  ips?: Record<string, number>;
  ip_geo?: Record<string, IpGeo>;
  countries?: Record<string, number>;
  cities?: Record<string, number>;
  devices?: Record<string, number>;
  asns?: Record<string, number>;
  apps?: Record<string, number>;
  client_apps?: Record<string, number>;
  auth_requirements?: Record<string, number>;
  /** Failure counters, kept apart from the success-only profile above. */
  failed_ips?: Record<string, number>;
  failed_countries?: Record<string, number>;
  /** Always empty: Azure AD sign-ins name no host. */
  hosts?: Record<string, number>;
  /** 24 slots, IST, per the baseline's own `tz_hour_histogram`. */
  hours_hist?: number[];
  /** Seven slots, index 0 = Monday. */
  dow_hist?: number[];
  /** Raw principals that resolved to this entity, by frequency. */
  identifiers?: Record<string, number>;
}

/** Sort a name→count map into a descending list with percentage shares. */
export function rank(map: Record<string, number> | undefined): { name: string; n: number; pct: number }[] {
  const entries = Object.entries(map ?? {});
  if (entries.length === 0) return [];
  const total = entries.reduce((s, [, n]) => s + n, 0) || 1;
  return entries
    .sort((a, b) => b[1] - a[1])
    .map(([name, n]) => ({ name, n, pct: Math.round((n / total) * 100) }));
}

/** The most-seen raw principal, or the entity key when the profile has none. */
export function displayAddress(profile: Profile, fallback: string): string {
  const ids = Object.entries(profile.identifiers ?? {}).sort((a, b) => b[1] - a[1]);
  const withAt = ids.find(([id]) => id.includes("@"));
  return (withAt ?? ids[0])?.[0] ?? fallback;
}

export function fromEntityPage(p: EntityPage): EntityVM {
  const profile = (p.profile ?? {}) as Profile;
  const hasProfile = Object.keys(profile).length > 0;

  return {
    ...baseEntity(p.entity, p.score, p.band),
    address: displayAddress(profile, p.entity),
    delta: Math.round(p.delta_24h ?? 0),
    // The true window count, not `p.anomalies.length`: that array is capped at
    // 250 by the builder, so a busy account's page used to contradict the
    // count on the Users roster.
    anomalies: p.n_anomalies ?? null,
    // Both from the baseline. Null when no profile came back at all — a
    // profiled account with no devices recorded is a real zero and says so.
    devices: hasProfile ? Object.keys(profile.devices ?? {}).length : null,
    ips: profile.distinct_ips ?? null,
  };
}

/**
 * The order an analyst reads the signals in: what was used, when, from where.
 * Also the order the detector's own notes list them.
 */
const SIGNAL_ORDER = ["app", "hour", "device", "country", "ip"] as const;

/** Human labels for the rows. `ip` keeps its acronym. */
const SIGNAL_LABELS: Record<(typeof SIGNAL_ORDER)[number], string> = {
  app: "app",
  hour: "hour",
  device: "device",
  country: "country",
  ip: "IP",
};

/**
 * The baseline comparison the detector recorded, one row per signal.
 *
 * Read from `evidence.reasons[].investigation` — the first reason carrying one,
 * since `normalize.anomaly` puts the primary reason first. Aggregate detections
 * (`credential_attack_volume`, `failed_signin_burst`) compare a window rather
 * than a single sign-in and carry no investigation at all; an empty list is the
 * normal answer for those, not a failure, and the card hides the section.
 */
function extractSignals(evidence: Anomaly["evidence"], detectionId: string): SignalRow[] {
  const reasons = (evidence?.reasons as AnomalyReason[] | undefined) ?? [];
  const inv: Investigation | undefined = reasons.find((r) => r?.investigation)?.investigation;
  if (!inv) return [];

  const rows: SignalRow[] = [];
  for (const key of SIGNAL_ORDER) {
    const check: SignalCheck | undefined = inv[key];
    if (!check || check.observed == null || check.observed === "") continue;
    rows.push({
      signal: SIGNAL_LABELS[key],
      observed: observedText(key, check),
      status: check.status ?? null,
      baseline: baselinePhrase(key, check),
      // The signal that raised *this* finding, by the detector's own account.
      flagged: !!check.would_flag_as && check.would_flag_as === detectionId,
      typical: (check.usual ?? []).slice(0, 3).map(String).join(", "),
    });
  }
  return rows;
}

/** An hour is a bucket, not a value: name the zone it was bucketed in. */
function observedText(key: string, check: SignalCheck): string {
  const raw = String(check.observed);
  if (key === "hour") return `${raw}:00${check.timezone ? ` ${check.timezone}` : ""}`;
  return raw;
}

/** How this signal compares to the baseline, in the terms it was measured in. */
function baselinePhrase(key: string, check: SignalCheck): string {
  // An address is compared by network membership, not by frequency: the
  // detector records no share for it.
  if (key === "ip") {
    return [check.asn, check.asn_category].filter(Boolean).join(" · ") || (check.note ?? "");
  }
  const pct = check.baseline_share_pct;
  const of = check.baseline_login_count;
  if (pct == null) return of != null ? `of ${of} sign-ins` : "";
  // The detector reports three decimals (73.418); one is all a reader uses.
  const shown = Number.isInteger(pct) ? String(pct) : pct.toFixed(1);
  return `${shown}%${of != null ? ` of ${of} sign-ins` : ""}`;
}

export function fromAnomaly(a: Anomaly, severityBands: SeverityBands): AnomalyVM {
  return {
    id: a.anomaly_id,
    incidentId: a.incident_id ?? a.anomaly_id,
    logUrl: a.log_url ?? null,
    signinUrl: a.signin_url ?? null,
    time: clockTime(a.ts),
    // The raw reason kind, not `a.name`. They are the same string on a current
    // snapshot, but `name` in a snapshot built before the relabel still holds
    // the old curated prose, and the console must never print that.
    type: a.detection_id || a.name,
    kind: a.detection_kind ?? null,
    entity: a.entity ?? null,
    detail: a.observed
      ? `${a.observed}${a.baseline ? ` (baseline ${a.baseline})` : ""}`
      : a.detection_id || a.name,
    points: Math.round(a.weight),
    sev: weightToSev(a.weight, severityBands),
    mitre: a.mitre_technique || null,
    // `evidence.source` is the detection's raw kind, set by `normalize.anomaly`.
    // Falls back to the id for the same stale-snapshot reason as `type`.
    source: (a.evidence?.source as string | undefined) ?? a.detection_id ?? a.name,
    eventIds: a.event_ids ?? [],
    signals: extractSignals(a.evidence, a.detection_id),
  };
}

