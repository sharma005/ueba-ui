import type { Band, Sev } from "../theme";

/**
 * One entity, as the screens render it.
 *
 * Every field here is either straight off the API or pure presentation
 * (`initials`, `color`). There used to be `kind`, `name`, `dept`, `title` and
 * `loc` as well, all synthesized from the identifier string: `kind` guessed
 * "Service" from an `svc-`/`rpa-` prefix, `name` title-cased the email local
 * part into a fake human name, and `dept` held "Peer group N" — a value this
 * pipeline never computes. Azure AD sign-in data carries no directory
 * attributes, so the resolved address is the only name there is.
 */
export interface EntityVM {
  /** Stable key used in URLs — the pipeline's `entity` field, and the label. */
  id: string;
  initials: string;
  /** The address to show: the profile's most-seen identifier, else `id`. */
  address: string;
  /** The server's own band, so no screen re-derives severity from the score. */
  band: Band;
  score: number;
  delta: number;
  /** `n_anomalies` for the scoring window. Null when the API did not say. */
  anomalies: number | null;
  /** Relative time of the last finding, or null if there has never been one. */
  last: string | null;
  /** Avatar colour — deterministic per entity, see `avatarColor`. */
  color: string;
  /** Distinct devices in the baseline. Null when no profile was returned. */
  devices: number | null;
  /** `profile.distinct_ips`. Null when absent — never counted from findings. */
  ips: number | null;
}

/** One row of the anomaly card's signal table. See `adapt.extractSignals`. */
export interface SignalRow {
  signal: string;
  observed: string;
  /** `"new" | "rare" | "usual"` as the detector wrote it, or null. */
  status: string | null;
  /** How it compares to baseline, phrased in the units it was measured in. */
  baseline: string;
  /** True on the signal whose `would_flag_as` matches this finding. */
  flagged: boolean;
  /** Up to three values that were normal for this account. */
  typical: string;
}

export interface AnomalyVM {
  id: string;
  /** Shown verbatim so it can be pasted into a Coralogix search. */
  incidentId: string;
  /** Record-level `detection_kind`, raw. Detail view only — see `Anomaly`. */
  kind: string | null;
  logUrl?: string | null;
  signinUrl?: string | null;
  time: string | null;
  /** The raw reason kind from the log (`new_app`), never curated prose. */
  type: string;
  /** The entity key, or null on a finding about an address rather than a person. */
  entity: string | null;
  detail: string;
  points: number;
  sev: Sev;
  /** Null when the catalog mapped no technique. */
  mitre: string | null;
  /** The detection's display name from the catalog. */
  source: string;
  /** `sample_event_ids` — the Azure AD sign-ins grouped into this finding. */
  eventIds: string[];
  /** Empty on aggregate detections, which carry no per-signal comparison. */
  signals: SignalRow[];
}

/** Deterministic avatar colour so an entity keeps the same hue across screens. */
const AVATAR_HUES = [
  "#E8734A", "#9B7BEA", "#4AA8E0", "#2FB7A8", "#E05A8F",
  "#E89B2B", "#6E80EA", "#31B87A", "#8B6FE0", "#4E9CD4",
  "#A06FE0", "#2AA98F", "#4E7CE0",
];

export function avatarColor(key: string): string {
  let h = 0;
  for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) >>> 0;
  return AVATAR_HUES[h % AVATAR_HUES.length];
}

/**
 * Avatar initials from the identifier. Presentation only — it makes no claim
 * about a person's name, which is why the address is still shown in full.
 */
export function initialsOf(name: string): string {
  const clean = name.split("@")[0].replace(/[._-]+/g, " ").trim();
  const parts = clean.split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

/**
 * Epoch millis → "2m ago" / "4h ago" / "3d ago", or null when there is no
 * timestamp. Null rather than "—" so callers decide how absence reads; the
 * pipeline writes `last_anomaly_ts: 0` for an account that has never had a
 * finding, which is "never", not "unknown".
 */
export function relTime(ts: number | null | undefined, now = Date.now()): string | null {
  if (!ts) return null;
  const s = Math.max(0, Math.floor((now - ts) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/** Epoch millis → "01 Aug 2026" in the viewer's locale, or null if absent. */
export function dayLabel(ts: number | null | undefined): string | null {
  if (!ts) return null;
  return new Date(ts).toLocaleDateString(undefined, {
    day: "2-digit", month: "short", year: "numeric",
  });
}

/** Epoch millis → "09:31:04" in the viewer's locale, or null if absent. */
export function clockTime(ts: number | null | undefined): string | null {
  if (!ts) return null;
  return new Date(ts).toTimeString().slice(0, 8);
}

export const compact = (n: number): string =>
  n >= 1_000_000 ? `${(n / 1_000_000).toFixed(2)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
