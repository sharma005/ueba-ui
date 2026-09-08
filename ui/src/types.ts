export type Band = "notable" | "high" | "medium" | "low";

export interface Kpis {
  users_monitored: number;
  hosts_monitored: number;
  notable_entities: number;
  high_entities: number;
  anomalies_today: number;
  max_score: number;
}

export interface Overview {
  generated_at: string;
  day: string;
  kpis: Kpis;
  trend: { dt: string; anomalies: number }[];
  top_detections: { detection_id: string; name: string; count: number }[];
  mitre: { mitre_tactic: string; mitre_technique: string; count: number }[];
  bands: Record<Band, number>;
}

export interface TopDetection {
  detection_id: string;
  name: string;
  points: number;
  count: number;
}

export interface NotableEntity {
  entity: string;
  entity_type: "user" | "host";
  score: number;
  band: Band;
  delta_24h: number;
  n_anomalies_24h: number;
  top_detections: TopDetection[];
  sparkline: { dt: string; score: number }[];
}

/**
 * One baseline comparison the detector wrote for a single signal.
 *
 * These arrive inside `evidence.reasons[].investigation` — the detector's own
 * "what did this account usually do" record, not something re-derived here.
 * `would_flag_as` names the detection this signal *would* have raised, which is
 * how the signal that actually fired this finding is identified.
 */
export interface SignalCheck {
  observed?: string | number | null;
  /** `"new" | "rare" | "usual"`, verbatim from the detector. */
  status?: string | null;
  in_baseline?: boolean;
  baseline_count?: number;
  baseline_login_count?: number;
  baseline_share_pct?: number;
  would_flag_as?: string | null;
  usual?: (string | number)[];
  /** Hour only — the tz the hour was bucketed in, so "15" is unambiguous. */
  timezone?: string | null;
  /** IP only — it is compared by network rather than by frequency. */
  asn?: string | null;
  asn_category?: string | null;
  note?: string | null;
}

export interface Investigation {
  app?: SignalCheck;
  device?: SignalCheck;
  hour?: SignalCheck;
  country?: SignalCheck;
  ip?: SignalCheck;
  flagged_reasons?: string[];
}

/** An entry of `evidence.reasons`, carried verbatim by `normalize.anomaly`. */
export interface AnomalyReason {
  kind?: string;
  why?: string;
  investigation?: Investigation;
}

export interface Anomaly {
  anomaly_id: string;
  /** The detector's own id, as it appears in the Coralogix log record. */
  incident_id?: string;
  /** `sample_event_ids` — the Azure AD sign-ins grouped into this finding. */
  event_ids?: string[];
  log_url?: string | null;
  /** Deep link to the raw sign-ins behind the finding. Same caveat. */
  signin_url?: string | null;
  ts: number;
  /** The raw `reasons[].kind` from the log — the detection's identity. */
  detection_id: string;
  /** Same raw string as `detection_id`; see `serving/catalog.label`. */
  name: string;
  /**
   * The record-level `detection_kind`, verbatim. Much coarser than
   * `detection_id` (~97% of findings share one value), so it is shown on the
   * detail view for pivoting into Coralogix, never used to group.
   */
  detection_kind?: string | null;
  entity?: string;
  entity_type?: string;
  weight: number;
  observed: string;
  baseline: string;
  mitre_tactic: string;
  mitre_technique: string;
  evidence: Record<string, unknown>;
}

export interface TimelineItem extends Partial<Anomaly> {
  kind: "anomaly" | "event";
  ts: number;
  event_type?: string;
  outcome?: string;
  action?: string;
  target?: string;
  host?: string;
  src_ip?: string;
  geo_city?: string;
  geo_country?: string;
  app?: string;
  source?: string;
  bytes_out?: number;
}

export interface EntityPage {
  entity: string;
  entity_type: "user" | "host";
  score: number;
  band: Band;
  delta_24h: number;
  /** True count in the scoring window; `anomalies` below may be capped. */
  n_anomalies?: number;
  n_anomalies_24h?: number;
  /** True when `anomalies` holds fewer records than `n_anomalies`. */
  anomalies_truncated?: boolean;
  top_detections: TopDetection[];
  profile: Record<string, any>;
  score_history: { dt: string; score: number }[];
  anomalies: Anomaly[];
  timeline: TimelineItem[];
}

export interface IndexEntity {
  entity: string;
  entity_type: "user" | "host";
  score: number;
  band: Band;
  last_anomaly_ts: number;
  delta_24h: number;
  /** Findings in the scoring window — the same window the score is computed over. */
  n_anomalies: number;
  n_anomalies_24h: number;
}

export interface GraphData {
  entity: string;
  nodes: { id: string; type: string; score: number }[];
  links: { source: string; target: string; weight: number }[];
}

/** `GET /api/health` — what the engine reports about itself, not what the repo contains. */
export interface Health {
  ok: boolean;
  ts: string;
  /** The feed the detector reads, from the baseline it wrote. Empty if unknown. */
  sources: string[];
  detections_enabled: number;
  /** False when the API runs with `API_KEY` unset — auth is disabled, not passed. */
  api_key_required: boolean;
  /** False when `CORALOGIX_UI_BASE` is unset, so findings carry no log link. */
  log_links_configured?: boolean;
  snapshot_generated_at?: string | null;
  /**
   * Epoch ms the snapshot was built from — the instant the server's own
   * `n_anomalies` counts back from. Slice windows from this, never from
   * `Date.now()`, or client counts drift below the server's by the snapshot's
   * age. Null before the first snapshot lands.
   */
  snapshot_generated_at_ms?: number | null;
  /** Null when no snapshot has landed yet. */
  snapshot_age_seconds?: number | null;
  entities?: number | null;
  anomalies_30d?: number | null;
  baseline_generated_at?: string | null;
  users_with_baseline?: number | null;
  /**
   * The detector has not finished learning this tenant's history. It suppresses
   * findings for accounts it has not seen, so this is the difference between "a
   * quiet customer" and "a customer we cannot see yet".
   */
  baseline_incomplete?: boolean | null;
  /** How far back it has filled in so far, while still working backwards. */
  baseline_resume_before?: string | null;
  /**
   * Shard count the snapshot in hand was built with, and the count this code
   * defaults to. They can differ — builder and reader do not deploy together —
   * and readers must use the former. See `config.entity_shard`.
   */
  entity_shards?: number | null;
  entity_shards_expected?: number | null;
  /** What the detector says about itself, from its own run-summary records. */
  pipeline?: {
    last_run_at?: string | null;
    last_run_events_scored?: number | null;
    last_run_anomalies_emitted?: number | null;
    runs_seen?: number | null;
    baseline_status?: string | null;
    detector_id?: string | null;
  };
}

export interface ScoringParams {
  scoring: {
    half_life_days?: number;
    window_days?: number;
    /** Score -> band cutoffs. `derive.BANDS`. */
    bands?: { medium?: number; high?: number; notable?: number };
    /** Weight -> severity cutoffs. The lows of `catalog.SEVERITY_BANDS`. */
    severity_bands?: { critical?: number; error?: number; warning?: number };
  };
}
