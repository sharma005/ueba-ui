import { useEffect, useRef, type ReactNode } from "react";
import type { Health, ScoringParams } from "../types";
import * as I from "./icons";
import type { TenantInfo } from "./Shell";
import { cardSurface } from "./primitives";

const DASH = "—";

/**
 * Read-only view of what the pipeline is configured with and how it is running.
 *
 * Everything here is already fetched by `Shell` for its own use — `/api/tuning`
 * for the cutoffs every screen colours by, `/api/health` for the status chip —
 * so opening this costs no request. It is deliberately a viewer and not a
 * settings screen: the serving layer exposes no mutation route, and the numbers
 * belong to `derive.py` and `catalog.py`, not to the console.
 *
 * The scrim/dialog animation classes come from `index.css`, which has carried
 * them under an "Admin gate" comment since before this component existed.
 */
export function AdminPanel({
  open,
  onClose,
  tuning,
  health,
  tenant,
}: {
  open: boolean;
  onClose: () => void;
  tuning: ScoringParams | null;
  health: Health | null;
  tenant: TenantInfo;
}) {
  const dialog = useRef<HTMLDivElement>(null);
  const restoreFocus = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    restoreFocus.current = document.activeElement as HTMLElement | null;
    dialog.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      restoreFocus.current?.focus?.();
    };
  }, [open, onClose]);

  if (!open) return null;

  const s = tuning?.scoring;
  const b = s?.bands;
  const sb = s?.severity_bands;
  const shards = health?.entity_shards;
  const shardsExpected = health?.entity_shards_expected;
  const shardMismatch =
    shards != null && shardsExpected != null && shards !== shardsExpected;

  return (
    <div
      className="fade-in fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 backdrop-blur-[2px]"
      onClick={onClose}
    >
      <div
        ref={dialog}
        role="dialog"
        aria-modal="true"
        aria-label="Pipeline configuration"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        className={`lift-in max-h-[85vh] w-full max-w-[560px] overflow-y-auto outline-none ${cardSurface}`}
      >
        <div className="flex items-center justify-between border-b border-line px-4 py-3">
          <div>
            <div className="text-[13px] font-semibold text-ink">Pipeline configuration</div>
            <div className="text-[11px] text-ink-3">
              Served read-only from <span className="font-mono">/api/tuning</span> and{" "}
              <span className="font-mono">/api/health</span>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="cursor-pointer rounded p-1 text-ink-3 transition-colors hover:bg-hover hover:text-ink"
          >
            <I.Chevron size={16} />
          </button>
        </div>

        <Section title="Tenant">
          <Row label="Selected" value={tenant.label} />
          <Row label="Identity provider" value={tenant.product} />
        </Section>

        <Section title="Scoring">
          <Row label="Half-life" value={num(s?.half_life_days, "d")} />
          <Row label="Window" value={num(s?.window_days, "d")} />
          <Row label="Notable at" value={num(b?.notable)} />
          <Row label="High at" value={num(b?.high)} />
          <Row label="Medium at" value={num(b?.medium)} />
        </Section>

        <Section title="Severity cutoffs" meta="finding weight → severity">
          <Row label="Critical at" value={num(sb?.critical)} />
          <Row label="High at" value={num(sb?.error)} />
          <Row label="Medium at" value={num(sb?.warning)} />
        </Section>

        <Section title="Detector">
          <Row label="Detector" value={health?.pipeline?.detector_id ?? DASH} mono />
          <Row label="Last run" value={health?.pipeline?.last_run_at ?? DASH} mono />
          <Row label="Runs seen" value={num(health?.pipeline?.runs_seen)} />
          <Row label="Last run status" value={health?.pipeline?.baseline_status ?? DASH} />
          <Row label="Baseline written" value={health?.baseline_generated_at ?? DASH} mono />
          {/* Separate from the run status above, and often disagreeing with it:
              a run can finish "ok" having written a baseline that still covers
              only part of the customer's history. */}
          <Row
            label="Baseline coverage"
            value={
              health?.baseline_incomplete == null
                ? DASH
                : health.baseline_incomplete
                  ? `incomplete${health.baseline_resume_before
                      ? ` — filling in before ${health.baseline_resume_before.slice(0, 10)}`
                      : ""}`
                  : "complete"
            }
          />
          <Row label="Accounts learned" value={num(health?.users_with_baseline)} />
          <Row label="Detections enabled" value={num(health?.detections_enabled)} />
          {/* The pipeline's own self-report, straight from the baseline
              document. It is not always the tenant's real provider: the Okta
              detector is a fork of the Azure one and still writes
              `source_application: "Azure"`, so this can read
              "Azure/Okta-audit/signin". The Tenant section above carries the
              truth; this row is labelled as the pipeline's claim so the two do
              not silently contradict each other. */}
          <Row
            label="Sources (as reported)"
            value={health?.sources?.length ? health.sources.join(", ") : DASH}
            mono
          />
        </Section>

        <Section title="Snapshot">
          <Row label="Built" value={health?.snapshot_generated_at ?? DASH} mono />
          <Row label="Age" value={age(health?.snapshot_age_seconds)} />
          <Row label="Entities" value={num(health?.entities)} />
          <Row label="Anomalies (30d)" value={num(health?.anomalies_30d)} />
          <Row
            label="Entity shards"
            value={
              shards == null
                ? DASH
                : shardMismatch
                  ? `${shards} — builder wrote ${shards}, this code expects ${shardsExpected}`
                  : `${shards}`
            }
            warn={shardMismatch}
          />
        </Section>

        <Section title="Access">
          <Row label="API key required" value={yesNo(health?.api_key_required)} />
          <Row label="Log links" value={yesNo(health?.log_links_configured)} />
        </Section>

        {shardMismatch && (
          <div className="border-t border-line px-4 py-3 text-[11px] leading-relaxed text-ink-3">
            The snapshot in S3 was written by a builder using {shards} shards while this code
            defaults to {shardsExpected}. Entity pages still resolve — readers use the count the
            snapshot records — but a rebuild will clear the mismatch.
          </div>
        )}
      </div>
    </div>
  );
}

function Section({ title, meta, children }: { title: string; meta?: string; children: ReactNode }) {
  return (
    <div className="border-b border-line last:border-b-0">
      <div className="flex items-baseline gap-2 px-4 pt-3 pb-1.5">
        <span className="caps text-ink-2">{title}</span>
        {meta && <span className="text-[10.5px] text-ink-3">{meta}</span>}
      </div>
      <dl className="grid grid-cols-[10rem_minmax(0,1fr)] gap-x-3 gap-y-1 px-4 pb-3 text-[12px]">
        {children}
      </dl>
    </div>
  );
}

function Row({
  label,
  value,
  mono,
  warn,
}: {
  label: string;
  value: string;
  mono?: boolean;
  warn?: boolean;
}) {
  return (
    <div className="contents">
      <dt className="text-ink-3">{label}</dt>
      <dd
        className={`min-w-0 break-words ${mono ? "font-mono text-[11.5px]" : ""} ${
          warn ? "text-high" : "text-ink"
        }`}
      >
        {value}
      </dd>
    </div>
  );
}

const num = (n: number | null | undefined, suffix = "") =>
  n == null ? DASH : `${n}${suffix}`;

const yesNo = (b: boolean | null | undefined) => (b == null ? DASH : b ? "yes" : "no");

function age(seconds: number | null | undefined): string {
  if (seconds == null) return DASH;
  const m = Math.round(seconds / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}
