import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Pad, Page } from "../components/Page";
import { Btn, Card, cardSurface, EmptyState, Select, SevBadge } from "../components/primitives";
import { useMode, useTenant, useTuning, useWindow } from "../components/Shell";
import { fromAnomaly } from "../data/adapt";
import { useResource } from "../data/useResource";
import { sevColor, SHOW_SCORING, themeColors } from "../theme";

const SEVS = ["All Severities", "Critical", "High", "Medium", "Low"];

/** Rows fetched per request. The true window total comes from the API. */
const PAGE_LIMIT = 1000;

/**
 * A signal's status painted with the severity palette, so "new" reads as
 * urgently as a critical finding and "usual" recedes. No new colours: these are
 * the same values `sevColor` serves the rest of the console.
 */
function statusColor(status: string | null, c: ReturnType<typeof themeColors>): string {
  if (status === "new") return sevColor("Critical", c);
  if (status === "rare") return sevColor("High", c);
  return "var(--text-muted)";
}

export default function Anomalies() {
  const nav = useNavigate();
  const c = themeColors(useMode());
  const { windowDays, window: winLabel } = useWindow();
  const { tuning } = useTuning();
  const { tenant } = useTenant();
  const [sev, setSev] = useState(SEVS[0]);

  const severityBands = tuning?.severityBands;
  const rows = useResource(
    () =>
      api
        .anomalies({ days: String(windowDays), limit: String(PAGE_LIMIT) })
        .then((r) => ({
          items: r.anomalies.map((a) => fromAnomaly(a, severityBands!)),
          // The window's true count, which the builder computes carefully and
          // this page used to throw away in favour of the row count.
          total: r.total,
          truncated: r.truncated ?? false,
          totalIsEstimate: r.total_is_estimate ?? false,
        })),
    // Waits for the ladder rather than colouring rows with a guessed one.
    [windowDays, severityBands, tenant],
  );

  const visible = useMemo(
    () => (sev === SEVS[0] ? rows.data?.items : rows.data?.items.filter((a) => a.sev === sev)),
    [rows.data, sev],
  );

  const subtitle = (() => {
    if (!rows.data) return undefined;
    const { total, truncated, totalIsEstimate } = rows.data;
    const shown = visible?.length ?? 0;
    const approx = totalIsEstimate ? "~" : "";
    const scope = `${approx}${total} in ${winLabel.toLowerCase()}`;
    // Say plainly when the feed did not return every row for the window: the
    // severity filter and any client-side tally only ever saw this slice.
    return truncated
      ? `${shown} shown of ${scope} — the feed returned the most recent ${PAGE_LIMIT}`
      : `${shown} of ${scope}`;
  })();

  return (
    <Page
      title="Anomaly Feed"
      subtitle={subtitle}
      source={rows.source}
      error={rows.error}
      backTo="/"
      onRefresh={rows.reload}
      actions={
        SHOW_SCORING ? (
          <div className="flex flex-1 items-center justify-end gap-2">
            <Select value={sev} onChange={setSev} options={SEVS} label="Severity" />
          </div>
        ) : undefined
      }
    >
      <Pad>
        {rows.data?.totalIsEstimate && (
          <p className="text-[11px] text-ink-3">
            The total is marked an estimate by the API: this window reaches past the stored
            feed, so it comes from the builder's per-day tally, which counts whole UTC days.
          </p>
        )}

        {!visible || visible.length === 0 ? (
          <Card>
            <EmptyState
              title={rows.source === "error" ? "Could not load the feed" : "No anomalies in this window"}
              hint={
                rows.source === "error"
                  ? (rows.error ?? "The API did not respond.")
                  : "Either the environment is inside baseline, or the detector has not run since deployment."
              }
            />
          </Card>
        ) : (
          visible.map((a) => (
            <div
              key={a.id}
              className={`overflow-hidden ${cardSurface}`}
              style={{ borderLeft: `3px solid ${sevColor(a.sev, c)}` }}
            >
              <div className="flex items-start gap-4 px-4 py-3">
                <div className="min-w-0 flex-1">
                  <div className="truncate font-mono text-[13.5px] font-bold text-ink">{a.type}</div>
                  {a.entity ? (
                    <button
                      type="button"
                      onClick={() => nav(`/users/${encodeURIComponent(a.entity!)}`)}
                      className="cursor-pointer font-mono text-[12px] font-semibold text-accent hover:underline"
                    >
                      {a.entity}
                    </button>
                  ) : (
                    // Spray findings are about an address, not a person, and
                    // carry no entity. Named rather than shown as an em-dash.
                    <span className="text-[12px] text-ink-3">no account — finding is about a source address</span>
                  )}
                </div>
                <div className="flex shrink-0 items-center gap-3">
                  {SHOW_SCORING && (
                    <span className="font-mono text-[12px] font-bold" style={{ color: sevColor(a.sev, c) }}>
                      +{a.points}
                    </span>
                  )}
                  <SevBadge sev={a.sev} />
                  {a.time && <span className="font-mono text-[11px] text-ink-3">{a.time}</span>}
                </div>
              </div>

              <div className="px-4 pb-3">
                <dl className="grid grid-cols-[5.5rem_minmax(0,1fr)] gap-x-2 gap-y-1 rounded-md bg-hover px-3 py-2.5 font-mono text-[11.5px] leading-relaxed text-ink-2">
                  {([
                    ["detection", a.source],
                    // The record-level `detection_kind` from the log, verbatim.
                    // Coarse (most findings share one value) so it is never the
                    // headline, but it is the field to search Coralogix on.
                    ["detection_kind", a.kind],
                    ["observed", a.detail],
                    ["mitre", a.mitre],
                    ["severity", SHOW_SCORING ? `${a.sev} (+${a.points} pts)` : null],
                    // The Azure AD sign-ins the detector grouped into this
                    // finding. Served as `event_ids` and never shown before.
                    ["sign-ins", a.eventIds.length > 0 ? a.eventIds.join(", ") : null],
                  ] as [string, string | null][])
                    .filter(([, value]) => value != null)
                    .map(([label, value]) => (
                      <div key={label} className="contents">
                        <dt className="text-ink-3">{label}:</dt>
                        <dd className="min-w-0 break-words">{value}</dd>
                      </div>
                    ))}
                </dl>

                {/* Native <details> so each row keeps its own open state
                    without React state across a thousand cards. Absent on
                    aggregate detections, which compare a window rather than a
                    single sign-in and carry no per-signal investigation. */}
                {a.signals.length > 0 && (
                  <details className="mt-1.5 group">
                    <summary className="cursor-pointer list-none text-[11.5px] font-semibold text-accent hover:underline">
                      <span className="mr-1 inline-block transition-transform group-open:rotate-90">›</span>
                      Signals &amp; context ({a.signals.length})
                    </summary>
                    <div className="mt-1.5 overflow-hidden rounded-md bg-hover font-mono text-[11.5px] text-ink-2">
                      <div className="grid grid-cols-[4.5rem_minmax(0,1.4fr)_4.5rem_minmax(0,1fr)] gap-x-2 border-b border-line-2 px-3 py-1.5 text-[10.5px] uppercase tracking-wide text-ink-3">
                        <span>signal</span>
                        <span>observed</span>
                        <span>status</span>
                        <span>vs baseline</span>
                      </div>
                      {a.signals.map((s) => (
                        <div
                          key={s.signal}
                          title={s.typical ? `Usually: ${s.typical}` : undefined}
                          className="grid grid-cols-[4.5rem_minmax(0,1.4fr)_4.5rem_minmax(0,1fr)] gap-x-2 px-3 py-1.5"
                          style={
                            // The signal that actually raised this finding,
                            // marked in the finding's own severity colour.
                            s.flagged ? { borderLeft: `2px solid ${sevColor(a.sev, c)}`, marginLeft: -2 } : undefined
                          }
                        >
                          <span className="text-ink-3">
                            {s.signal}
                            {s.flagged && (
                              <span title="The signal that raised this finding" className="ml-1 text-accent">
                                *
                              </span>
                            )}
                          </span>
                          <span className="min-w-0 break-words">{s.observed}</span>
                          <span
                            className="font-semibold"
                            style={{ color: statusColor(s.status, c) }}
                          >
                            {s.status ?? "—"}
                          </span>
                          <span className="min-w-0 break-words text-ink-3">{s.baseline || "—"}</span>
                        </div>
                      ))}
                    </div>
                  </details>
                )}
              </div>

              <div className="flex flex-wrap items-center gap-2 border-t border-line-2 px-4 py-2">
                <span className="mr-auto flex min-w-0 items-center gap-1.5 text-[11px] text-ink-3">
                  <span className="shrink-0">incident</span>
                  <code className="min-w-0 select-all truncate font-mono text-[11px] text-ink-2">
                    {a.incidentId}
                  </code>
                </span>
                {a.logUrl && (
                  <a
                    href={a.logUrl}
                    target="_blank"
                    rel="noreferrer noopener"
                    title="Open this finding's log record in Coralogix"
                    className="text-[11.5px] font-semibold text-accent hover:underline"
                  >
                    View log ↗
                  </a>
                )}
                {a.signinUrl && (
                  <a
                    href={a.signinUrl}
                    target="_blank"
                    rel="noreferrer noopener"
                    title="Open the raw Azure AD sign-ins behind this finding"
                    className="text-[11.5px] font-semibold text-accent hover:underline"
                  >
                    Sign-ins ↗
                  </a>
                )}
                {a.entity && (
                  <Btn size="sm" onClick={() => nav(`/users/${encodeURIComponent(a.entity!)}`)}>
                    Open entity
                  </Btn>
                )}
              </div>
            </div>
          ))
        )}
      </Pad>
    </Page>
  );
}
