import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Donut, type Slice } from "../components/charts";
import { Pad, Page } from "../components/Page";
import { Avatar, Badge, Bar, Card, CardHead, EmptyState, Notice, SevBadge, StatBar } from "../components/primitives";
import { useMode, useTenant, useTuning, useWindow } from "../components/Shell";
import * as I from "../components/icons";
import { fromAnomaly, fromNotable } from "../data/adapt";
import { compact, type EntityVM } from "../data/models";
import { useResource, worstSource } from "../data/useResource";
import { bandToSev, categorical, levelOf, sevColor, SHOW_SCORING, themeColors } from "../theme";

/** Rows the donut is tallied from. The window total comes from the API. */
const FEED_LIMIT = 1000;

/**
 * A figure the API did not send reads as "—", never as 0.
 *
 * This page used to start from a zeroed `EMPTY_OVERVIEW`, so a missing
 * snapshot or a failed request rendered "0 users, 0 anomalies, highest score
 * 0, LOW" — indistinguishable from a genuinely quiet tenant.
 */
const DASH = "—";
const num = (v: number | null | undefined) => (v == null ? DASH : compact(v));

export default function Dashboard() {
  const nav = useNavigate();
  const { tuning } = useTuning();
  const { window: winLabel, windowDays } = useWindow();
  const { tenant } = useTenant();
  const mode = useMode();
  const c = themeColors(mode);
  const [q, setQ] = useState("");

  const severityBands = tuning?.severityBands;

  const ov = useResource(() => api.overview(), [tenant]);
  const notables = useResource(
    () => api.notables().then((r) => (r.entities ?? []).map(fromNotable)),
    [tenant],
  );
  const anomalies = useResource(
    () =>
      api.anomalies({ days: String(windowDays), limit: String(FEED_LIMIT) }).then((r) => ({
        rows: r.anomalies.map((a) => fromAnomaly(a, severityBands!)),
        total: r.total,
        truncated: r.truncated ?? false,
        totalIsEstimate: r.total_is_estimate ?? false,
      })),
    [windowDays, severityBands, tenant],
  );
  const source = worstSource(ov.source, notables.source, anomalies.source);
  const failure = ov.error ?? notables.error ?? anomalies.error;
  const k = ov.data?.kpis;
  // `/overview` answers `{}` when the builder has not written overview.json.
  const noArtifact = ov.source === "live" && !ov.data?.generated_at;

  const slices: Slice[] = useMemo(() => {
    const palette = categorical(c);
    const counts: Record<string, number> = {};
    for (const a of anomalies.data?.rows ?? []) counts[a.type] = (counts[a.type] ?? 0) + 1;
    return Object.entries(counts)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 6)
      .map(([label, value], i) => ({ label, value, color: palette[i % palette.length] }));
  }, [anomalies.data, c]);

  const topEntity = useMemo(
    () =>
      (notables.data ?? []).reduce<EntityVM | null>(
        (best, e) => (best === null || e.score > best.score ? e : best),
        null,
      ),
    [notables.data],
  );

  // `/notables` is already ranked by the builder; this only bounds the list.
  const topUsers = useMemo(() => (notables.data ?? []).slice(0, 8), [notables.data]);

  const stale = useMemo(() => {
    const day = ov.data?.day;
    if (!day) return null;
    const today = new Date().toISOString().slice(0, 10);
    if (day >= today) return null;
    const lag = Math.round(
      (Date.parse(`${today}T00:00:00Z`) - Date.parse(`${day}T00:00:00Z`)) / 86_400_000,
    );
    return { day, lag };
  }, [ov.data]);

  const search = (e: React.FormEvent) => {
    e.preventDefault();
    nav(`/users?q=${encodeURIComponent(q)}`);
  };

  // Only for `max_score`, which arrives as a bare number. Every row below
  // reads the server's own `band` instead of re-deriving one from the score.
  const maxLevel =
    k?.max_score != null && tuning ? levelOf(k.max_score, tuning.bands) : null;

  return (
    <Page
      title={SHOW_SCORING ? "Risk Dashboard" : "Dashboard"}
      source={source}
      error={failure}
      onRefresh={() =>
        Promise.all([ov.reload(), notables.reload(), anomalies.reload()])
      }
      actions={
        <form onSubmit={search} className="relative min-w-0 flex-1">
          <I.Search
            size={14}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-3"
          />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search accounts…"
            aria-label="Search entities"
            className="w-full rounded-md border border-line bg-input py-2 pl-9 pr-3 text-[12.5px] text-ink outline-none transition-colors placeholder:text-ink-3 focus:border-accent"
          />
        </form>
      }
    >
      <Pad>
        {noArtifact && (
          <Notice>
            No <span className="font-mono">serving/overview.json</span> under this
            data base yet, so the accounts, highest score and alert counts read “—”. Run the
            pipeline (serving builder) to populate them — the anomaly panels below
            query their own source and are unaffected.
          </Notice>
        )}

        {stale && (
          <Notice>
            Accounts, highest score and alert counts come from the{" "}
            <span className="font-semibold">{stale.day}</span> pipeline run —{" "}
            {stale.lag === 1 ? "one day" : `${stale.lag} days`} behind now. The anomaly
            count and the panels below are a live rolling window, so they will not
            add up to that run's daily totals.
          </Notice>
        )}

        <StatBar
          stats={[
            {
              label: "Accounts",
              // Everyone the detector has a behavioural profile for. There is
              // no hosts figure beside it any more: Azure AD sign-ins name no
              // host, so `hosts_monitored` is hardcoded 0 upstream.
              value: num(k?.users_monitored),
              sub: "with a learned baseline",
              onClick: () => nav("/users"),
            },
            {
              label: "Anomalies",
              // Not `k.anomalies_today`: that is a calendar day, this is the
              // rolling window the header selected.
              value: anomalies.data
                ? `${anomalies.data.totalIsEstimate ? "~" : ""}${compact(anomalies.data.total)}`
                : DASH,
              sub: winLabel.toLowerCase(),
              // Uncoloured when there is no figure: an orange or green dash
              // still reads as a verdict on a value that was never returned.
              color: SHOW_SCORING && anomalies.data ? c.med : undefined,
              onClick: () => nav("/anomalies"),
            },
            {
              label: SHOW_SCORING ? "Highest score" : "Top account",
              // With scoring hidden the tile keeps its slot and its
              // click-through, but the figure and band are the scoring itself.
              value: !SHOW_SCORING || k?.max_score == null ? DASH : Math.round(k.max_score),
              sub: SHOW_SCORING
                ? [maxLevel?.label.toLowerCase(), topEntity?.id].filter(Boolean).join(" · ") || undefined
                : (topEntity?.id ?? undefined),
              color: SHOW_SCORING && maxLevel ? sevColor(maxLevel.sev, c) : undefined,
              title: topEntity ? `Open ${topEntity.id}` : "Browse accounts",
              onClick: () =>
                nav(topEntity ? `/users/${encodeURIComponent(topEntity.id)}` : "/users"),
            },
            {
              label: SHOW_SCORING ? "Alerts" : "Flagged",
              // Still the `notable_entities` score-band tally:
              // there is no alert store, and the Alerts page defines an alert as
              // exactly an account at or above `bands.notable`. Not the sidebar's
              // filtered `/notables` count — that list is capped at NOTABLE_LIMIT
              // (top 50) by the builder, so it would read 50 where this reads 510.
              value: SHOW_SCORING ? num(k?.notable_entities) : DASH,
              color: SHOW_SCORING && k?.notable_entities ? c.crit : undefined,
              onClick: () => nav("/alerts"),
            },
          ]}
        />

        {/* The "Top Risky Hosts" panel that used to sit beside this is gone:
            Azure AD sign-ins carry no host entity, so `/entities?type=host`
            can only ever return nothing.

            One row of three on a wide screen, two then one as it narrows:
            `items-start` is deliberately absent so the three cards match
            heights and their internal scroll areas line up. */}
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          <Card className="flex min-h-[330px] flex-col">
            <CardHead
              title={SHOW_SCORING ? "Notable Accounts" : "Top Accounts"}
              meta={SHOW_SCORING ? "Ranked by risk score" : undefined}
            />
            <div className="min-h-0 flex-1 overflow-y-auto">
              {notables.data == null ? (
                <EmptyState
                  title={notables.source === "error" ? "Could not load notables" : "Loading…"}
                  hint={notables.error ?? undefined}
                />
              ) : topUsers.length === 0 ? (
                <EmptyState title={SHOW_SCORING ? "No scored accounts" : "No accounts"} />
              ) : (
                topUsers.map((u, i) => <NotableRow key={u.id} u={u} rank={i + 1} />)
              )}
            </div>
          </Card>

          <Card className="flex min-h-[330px] flex-col">
            <CardHead
              title="Anomaly Types"
              // Says what was counted. The tally is over the rows this page
              // fetched, which is not the whole window when the feed truncates.
              meta={
                anomalies.data == null
                  ? undefined
                  : anomalies.data.truncated
                    ? `top types in the latest ${anomalies.data.rows.length} of ${compact(anomalies.data.total)}`
                    : `${anomalies.data.total} total`
              }
            />
            <div className="flex min-w-0 flex-1 items-center justify-center p-4">
              {anomalies.data == null ? (
                <EmptyState
                  title={anomalies.source === "error" ? "Could not load the feed" : "Loading…"}
                  hint={anomalies.error ?? undefined}
                />
              ) : slices.length === 0 ? (
                <EmptyState title={`No anomalies in ${winLabel.toLowerCase()}`} />
              ) : (
                <Donut slices={slices} onSliceClick={() => nav("/anomalies")} />
              )}
            </div>
          </Card>

          <Card className="flex min-h-[330px] flex-col">
            <CardHead title="Recent Anomalies" meta={winLabel} />
            <div className="min-h-0 flex-1 overflow-y-auto">
              {anomalies.data == null ? (
                <EmptyState
                  title={anomalies.source === "error" ? "Could not load the feed" : "Loading…"}
                  hint={anomalies.error ?? undefined}
                />
              ) : anomalies.data.rows.length === 0 ? (
                <EmptyState title="Nothing outside baseline" />
              ) : (
                anomalies.data.rows.slice(0, 8).map((a) => (
                  <button
                    key={a.id}
                    type="button"
                    onClick={() => a.entity && nav(`/users/${encodeURIComponent(a.entity)}`)}
                    className="flex w-full cursor-pointer items-start gap-2.5 border-b border-line-2 px-4 py-2.5 text-left transition-colors last:border-b-0 hover:bg-card-hover"
                  >
                    <span
                      className="mt-1.5 h-2 w-2 shrink-0 rounded-full"
                      style={{ background: sevColor(a.sev, c) }}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-mono text-[12.5px] font-semibold text-ink">{a.type}</span>
                      <span className="block truncate font-mono text-[11.5px] font-medium text-accent">
                        {a.entity ?? "no account"}
                      </span>
                      {a.time && <span className="block truncate text-[11px] text-ink-3">{a.time}</span>}
                    </span>
                    <SevBadge sev={a.sev} />
                  </button>
                ))
              )}
            </div>
          </Card>
        </div>
      </Pad>
    </Page>
  );
}

function NotableRow({ u, rank }: { u: EntityVM; rank: number }) {
  const nav = useNavigate();
  const c = themeColors(useMode());
  // The band the pipeline assigned, not a threshold comparison done here.
  const sev = bandToSev(u.band);
  const color = sevColor(sev, c);

  return (
    <button
      type="button"
      onClick={() => nav(`/users/${encodeURIComponent(u.id)}`)}
      className="flex w-full cursor-pointer items-center gap-3 border-b border-line-2 px-4 py-2.5 text-left transition-colors last:border-b-0 hover:bg-card-hover"
    >
      <span className="w-5 shrink-0 font-mono text-[11px] text-ink-3">#{rank}</span>
      <Avatar initials={u.initials} color={u.color} size={30} />
      <span className="min-w-0 flex-1">
        <span className="block truncate font-mono text-[12.5px] font-semibold text-ink">{u.id}</span>
        <span className="block truncate text-[11px] text-ink-3">
          {u.anomalies == null ? "" : `${u.anomalies} finding${u.anomalies === 1 ? "" : "s"} in 24h`}
        </span>
      </span>
      {SHOW_SCORING && (
        <>
          <Bar pct={u.score} color={color} className="w-12 shrink-0" />
          <span className="w-7 shrink-0 text-right font-mono text-[13px] font-bold" style={{ color }}>
            {u.score}
          </span>
          <Badge tone="neutral" className="shrink-0 border-none bg-transparent">
            <span style={{ color }}>{u.band.toUpperCase()}</span>
          </Badge>
        </>
      )}
    </button>
  );
}
