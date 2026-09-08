import { useMemo, useState } from "react";
import type { TimelineItem } from "../types";
import { sevColor, SHOW_SCORING, themeColors, weightToSev, type Sev, type SeverityBands } from "../theme";
import { useMode } from "./Shell";
import * as I from "./icons";

/**
 * The entity's **findings** over time, grouped for reading.
 *
 * Not a full activity log: only the detector's findings reach S3, raw Azure AD
 * sign-ins stay in Coralogix (see `routes.entity_events`). The session
 * clustering and the repeat rollup below are display groupings applied in the
 * browser — they are not something the pipeline computed — so the header says
 * as much rather than implying the backend sessionized anything.
 *
 * There used to be a `WELL_KNOWN_APPS` table here mapping seven Microsoft
 * application GUIDs to product names. Client-side enrichment covering seven of
 * thousands of app ids, guaranteed to go stale: the record's own value is shown
 * instead.
 */

/** A session break. A display grouping, not a pipeline concept. */
const SESSION_GAP_MS = 2 * 3600 * 1000;

export interface GroupedTimelineEvent {
  id: string;
  ts: number;
  timeStr: string;
  label: string;
  action: string;
  target: string;
  app: string;
  source: string;
  count: number;
  kind: "anomaly" | "event";
  /** Null when the record carried no weight — never a substituted default. */
  sev: Sev | null;
  points?: number;
  observed?: string;
  baseline?: string;
  mitre?: string;
  subEvents: TimelineItem[];
}

export interface ActivitySession {
  id: string;
  dateStr: string;
  dayOfWeek: string;
  events: GroupedTimelineEvent[];
}

function formatClockTime(ts: number): string {
  if (!ts) return "—";
  return new Date(ts).toTimeString().slice(0, 8);
}

/** Localised, unlike the hardcoded English day/month arrays this replaced. */
function formatDateHeader(ts: number): { dayOfWeek: string; dateStr: string } {
  const d = new Date(ts);
  return {
    dayOfWeek: d.toLocaleDateString(undefined, { weekday: "long" }),
    dateStr: d.toLocaleDateString(undefined, {
      day: "numeric", month: "short", year: "numeric",
    }),
  };
}

function getEventSignature(it: TimelineItem): string {
  if (it.kind === "anomaly") {
    return `anomaly|${it.detection_id || it.name}|${it.anomaly_id || it.ts}`;
  }
  return `event|${it.action || ""}|${it.target || ""}|${it.app || ""}|${it.source || ""}|${it.outcome || ""}|${it.host || ""}`;
}

export function groupTimelineIntoSessions(
  rawItems: TimelineItem[],
  severityBands: SeverityBands,
): ActivitySession[] {
  if (!rawItems || rawItems.length === 0) return [];

  // Sort chronologically ascending
  const sorted = [...rawItems].sort((a, b) => a.ts - b.ts);

  const sessionClusters: TimelineItem[][] = [];
  let currentCluster: TimelineItem[] = [];

  for (const item of sorted) {
    if (currentCluster.length === 0) {
      currentCluster.push(item);
      continue;
    }

    const lastItem = currentCluster[currentCluster.length - 1];
    const timeDiff = item.ts - lastItem.ts;
    const sameDay = new Date(item.ts).toDateString() === new Date(lastItem.ts).toDateString();

    // Session break on a gap or across midnight
    if (timeDiff > SESSION_GAP_MS || !sameDay) {
      sessionClusters.push(currentCluster);
      currentCluster = [item];
    } else {
      currentCluster.push(item);
    }
  }
  if (currentCluster.length > 0) {
    sessionClusters.push(currentCluster);
  }

  // 2. Build ActivitySession objects with event consolidation (x counter)
  const sessions: ActivitySession[] = sessionClusters.map((cluster, sIdx) => {
    const startTs = cluster[0].ts;
    const { dayOfWeek, dateStr } = formatDateHeader(startTs);

    // Consolidate consecutive / identical events within the session
    const groupedEvents: GroupedTimelineEvent[] = [];
    let currentGroup: TimelineItem[] = [];
    let currentSig = "";

    const flushGroup = () => {
      if (currentGroup.length === 0) return;
      const head = currentGroup[0];
      const count = currentGroup.length;
      const isAnomaly = head.kind === "anomaly";

      // The shared ladder from `/api/tuning`, and no invented weight: this
      // used to be `head.weight ?? 10` scored against its own 35/25/15
      // cutoffs, so a weight-30 critical finding rendered "High" here and
      // "Critical" on the anomaly feed, and a finding with no weight at all
      // was shown contributing +10 points that nothing had measured.
      const hasWeight = isAnomaly && typeof head.weight === "number";
      const points = hasWeight ? Math.round(head.weight as number) : undefined;
      const sev = points === undefined ? null : weightToSev(points, severityBands);

      const displayTarget = (head.target || head.app || "").trim();
      let label = "";
      if (isAnomaly) {
        // Raw kind first: `name` in a snapshot built before the relabel still
        // holds curated prose.
        label = head.detection_id || head.name || "Finding";
      } else {
        const actionPart = head.action || head.event_type || "Event";
        label = displayTarget ? `${actionPart} → ${displayTarget}` : actionPart;
      }

      groupedEvents.push({
        id: head.anomaly_id ?? `${head.ts}-${groupedEvents.length}`,
        ts: head.ts,
        timeStr: formatClockTime(head.ts),
        label,
        action: head.action || head.event_type || "",
        target: displayTarget,
        app: head.app || "",
        source: head.source || head.app || "—",
        count,
        kind: head.kind,
        sev,
        points,
        observed: head.observed,
        baseline: head.baseline,
        mitre: head.mitre_technique || head.mitre_tactic,
        subEvents: [...currentGroup],
      });
      currentGroup = [];
      currentSig = "";
    };

    for (const item of cluster) {
      const sig = getEventSignature(item);
      if (currentGroup.length === 0) {
        currentGroup = [item];
        currentSig = sig;
      } else if (sig === currentSig && item.kind !== "anomaly") {
        currentGroup.push(item);
      } else {
        flushGroup();
        currentGroup = [item];
        currentSig = sig;
      }
    }
    flushGroup();

    return {
      id: `session-${sIdx}-${startTs}`,
      dateStr,
      dayOfWeek,
      events: groupedEvents,
    };
  });

  // Return newest sessions first
  return sessions.reverse();
}

interface ActivityTimelineProps {
  timeline: TimelineItem[];
  severityBands: SeverityBands;
}

export function ActivityTimeline({ timeline, severityBands }: ActivityTimelineProps) {
  const c = themeColors(useMode());
  const sessions = useMemo(
    () => groupTimelineIntoSessions(timeline, severityBands),
    [timeline, severityBands],
  );
  const [expandedRows, setExpandedRows] = useState<Record<string, boolean>>({});

  const toggleRow = (id: string) => {
    setExpandedRows((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  if (!timeline || timeline.length === 0) {
    return (
      <div className="rounded-lg border border-line bg-card p-8 text-center text-ink-3">
        <I.Clock size={28} className="mx-auto mb-2 opacity-50" />
        <div className="text-[14px] font-semibold text-ink">No findings in this window</div>
        <div className="mt-1 text-[12px]">
          This is the detector's finding timeline, not a full sign-in log — raw sign-ins are
          not in the S3 store.
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-8">
      {sessions.map((session) => (
        <div
          key={session.id}
          className="overflow-hidden rounded-lg border border-line bg-card shadow-sm transition-all"
        >
          <div className="border-b border-line bg-surface/50 px-4 py-2.5 sm:px-5">
            <h3 className="text-[12.5px] font-semibold text-ink-2">
              {session.dayOfWeek}, {session.dateStr}
            </h3>
          </div>

          <div className="p-4 sm:p-6">
            <div className="relative border-l-2 border-line/80 pl-6 sm:pl-8 space-y-4">
              {session.events.map((ev) => {
                const isOpen = expandedRows[ev.id];
                const isAnomaly = ev.kind === "anomaly";
                // One severity, from `ev.sev`. This used to be a third set of
                // cutoffs (30/15) disagreeing with both the badge beside it
                // and the anomaly feed.
                const stripeColor = ev.sev
                  ? sevColor(ev.sev, c)
                  : isAnomaly
                    ? c.purple
                    : c.blue;

                return (
                  <div key={ev.id} className="relative group">
                    <div
                      className="absolute -left-[31px] sm:-left-[39px] top-3.5 h-3.5 w-3.5 rounded-full border-2 bg-surface transition-transform group-hover:scale-125"
                      style={{ borderColor: stripeColor }}
                    />

                    <div className="mb-1 flex items-center gap-2 font-mono text-[11px] text-ink-3">
                      <span>{ev.timeStr}</span>
                      {ev.count > 1 && (
                        <span className="rounded bg-surface px-1.5 py-0.2 font-semibold text-blue-400">
                          {ev.count} occurrences
                        </span>
                      )}
                    </div>

                    <div
                      className="overflow-hidden rounded-md border border-line bg-surface transition-all hover:border-line-2 hover:bg-card-hover"
                      style={{ borderLeftWidth: "4px", borderLeftColor: stripeColor }}
                    >
                      <div
                        onClick={() => toggleRow(ev.id)}
                        className="flex cursor-pointer flex-col gap-3 p-3.5 sm:flex-row sm:items-center sm:justify-between"
                      >
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-2">
                            {ev.count > 1 ? (
                              <span className="inline-flex items-center rounded bg-blue-500/20 px-2 py-0.5 font-mono text-[12px] font-bold text-blue-400">
                                {ev.count}x
                              </span>
                            ) : null}

                            <span className="text-[13px] font-semibold text-ink hover:text-accent">
                              {ev.label}
                            </span>
                          </div>

                          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-ink-3">
                            <span className="font-mono">{ev.source}</span>
                            {ev.subEvents[0]?.src_ip && (
                              <span>IP: <strong className="font-mono text-ink-2">{ev.subEvents[0].src_ip}</strong></span>
                            )}
                            {(ev.subEvents[0]?.geo_city || ev.subEvents[0]?.geo_country) && (
                              <span>
                                📍 {[ev.subEvents[0].geo_city, ev.subEvents[0].geo_country].filter(Boolean).join(", ")}
                              </span>
                            )}
                            {ev.subEvents[0]?.host && (
                              <span>Host: <strong className="font-mono text-ink-2">{ev.subEvents[0].host}</strong></span>
                            )}
                          </div>
                        </div>

                        <div className="flex shrink-0 items-center gap-3">
                          {SHOW_SCORING && ev.points !== undefined && (
                            <div
                              className="flex h-8 min-w-[44px] items-center justify-center rounded px-2 font-mono text-[13px] font-bold text-white shadow-sm"
                              style={{ background: stripeColor }}
                            >
                              +{ev.points}
                            </div>
                          )}

                          <button
                            type="button"
                            className="rounded p-1 text-ink-3 transition-colors hover:text-ink"
                            onClick={(e) => {
                              e.stopPropagation();
                              toggleRow(ev.id);
                            }}
                          >
                            <span className="font-mono text-[13px]">{isOpen ? "−" : "+"}</span>
                          </button>
                        </div>
                      </div>

                      {isOpen && (
                        <div className="border-t border-line/80 bg-card p-4 text-[12px] text-ink-2 space-y-3">
                          {ev.count > 1 && (
                            <div>
                              <div className="mb-2 text-[11px] font-bold uppercase tracking-wider text-ink-3">
                                Grouped Occurrences ({ev.count})
                              </div>
                              <div className="max-h-48 overflow-y-auto rounded border border-line bg-surface/80">
                                <table className="w-full text-left text-[11px]">
                                  <thead className="bg-thead font-mono text-ink-3">
                                    <tr>
                                      <th className="px-3 py-1.5">Time</th>
                                      <th className="px-3 py-1.5">Source IP</th>
                                      <th className="px-3 py-1.5">Location</th>
                                      <th className="px-3 py-1.5">Outcome</th>
                                      <th className="px-3 py-1.5">App / Target</th>
                                    </tr>
                                  </thead>
                                  <tbody className="divide-y divide-line">
                                    {ev.subEvents.map((sub, sIdx) => (
                                      <tr key={sIdx} className="hover:bg-hover font-mono">
                                        <td className="px-3 py-1.5 text-ink">{formatClockTime(sub.ts)}</td>
                                        <td className="px-3 py-1.5">{sub.src_ip || "—"}</td>
                                        <td className="px-3 py-1.5 font-sans">
                                          {[sub.geo_city, sub.geo_country].filter(Boolean).join(", ") || "—"}
                                        </td>
                                        <td className="px-3 py-1.5">
                                          <span className={sub.outcome === "success" ? "text-accent" : "text-crit"}>
                                            {sub.outcome || "—"}
                                          </span>
                                        </td>
                                        <td className="px-3 py-1.5 font-sans">{sub.target || sub.app || "—"}</td>
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              </div>
                            </div>
                          )}

                          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                            {ev.observed && (
                              <div className="rounded bg-surface p-2.5">
                                <span className="font-semibold text-ink">Observed:</span> {ev.observed}
                              </div>
                            )}
                            {ev.baseline && (
                              <div className="rounded bg-surface p-2.5">
                                <span className="font-semibold text-ink">Baseline:</span> {ev.baseline}
                              </div>
                            )}
                            {ev.mitre && (
                              <div className="rounded bg-surface p-2.5">
                                <span className="font-semibold text-ink">MITRE Technique:</span> {ev.mitre}
                              </div>
                            )}
                            <div className="rounded bg-surface p-2.5">
                              <span className="font-semibold text-ink">Source Feed:</span> {ev.source}
                            </div>
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
