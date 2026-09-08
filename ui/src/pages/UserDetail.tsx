import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { ActivityTimeline } from "../components/ActivityTimeline";
import { EntityGraph } from "../components/EntityGraph";
import { Pad, Page } from "../components/Page";
import {
  Avatar, Bar, Btn, Card, CardHead, EmptyState, SevBadge,
} from "../components/primitives";
import { useMode, useTenant, useTuning, useWindow } from "../components/Shell";
import * as I from "../components/icons";
import { fromAnomaly, fromEntityPage, rank, type IpGeo, type Profile } from "../data/adapt";
import { dayLabel, type AnomalyVM } from "../data/models";
import { useResource } from "../data/useResource";
import { bandToSev, sevColor, SHOW_SCORING, themeColors, type ScoreBands } from "../theme";
import type { GraphData } from "../types";

type Tab = "overview" | "timeline" | "graph";

const TABS: { key: Tab; label: string }[] = [
  { key: "overview", label: "Overview" },
  { key: "timeline", label: "Findings Timeline" },
  { key: "graph", label: "Relationships" },
];

export default function UserDetail() {
  const { id = "" } = useParams();
  const { tuning } = useTuning();
  const c = themeColors(useMode());
  const [tab, setTab] = useState<Tab>("overview");
  const { windowDays, since } = useWindow();
  const { tenant } = useTenant();

  const severityBands = tuning?.severityBands;
  const page = useResource(() => api.entity(id), [id, tenant]);

  const ent = page.data ? fromEntityPage(page.data) : null;
  const profile = (page.data?.profile ?? {}) as Profile;


  const rawTimeline = useMemo(
    () => (page.data?.timeline ?? []).filter((t) => t.ts >= since),
    [page.data, since],
  );

  /**
   * Findings inside the header window — Score Contributions follows the time
   * selector like every other panel. The headline score is still the
   * pipeline's own 30-day figure, so a short window can explain only part of
   * it; the panel's meta line names the window it is summarising.
   */
  const anomalies: AnomalyVM[] = useMemo(
    () =>
      severityBands
        ? (page.data?.anomalies ?? [])
            .filter((a) => a.ts >= since)
            .map((a) => fromAnomaly(a, severityBands))
        : [],
    [page.data, severityBands, since],
  );

  /**
   * One finding count for both the header and the Score Contributions meta.
   *
   * Counts raw records rather than `anomalies`, so it still reads correctly
   * when /api/tuning has not answered. `since` comes from `useWindow` and is
   * anchored to the snapshot's build clock, which is the same floor the
   * pipeline counted `n_anomalies` from — so at the full window this equals
   * the roster card's number instead of trailing it by the snapshot's age.
   */
  const windowFindings = useMemo(
    () => (page.data?.anomalies ?? []).filter((a) => a.ts >= since).length,
    [page.data, since],
  );

  const findingsLabel = page.data?.anomalies_truncated
    ? `${windowFindings} of ${page.data.n_anomalies} findings in ${windowDays}d`
    : `${windowFindings} finding${windowFindings === 1 ? "" : "s"} in ${windowDays}d`;

  const contributions = useMemo(() => {
    const byType = new Map<string, {
      type: string; sev: AnomalyVM["sev"]; points: number; count: number; detail: string;
    }>();
    for (const a of anomalies) {
      const seen = byType.get(a.type);
      if (!seen) {
        byType.set(a.type, {
          type: a.type, sev: a.sev, points: a.points, count: 1, detail: a.detail,
        });
      } else {
        seen.count += 1;
        // Occurrences of one kind can differ in severity; lead with the worst.
        if (a.points > seen.points) {
          seen.points = a.points;
          seen.sev = a.sev;
        }
      }
    }
    return [...byType.values()].sort((x, y) => y.points - x.points || y.count - x.count);
  }, [anomalies]);

  const maxPoints = Math.max(1, ...contributions.map((g) => g.points));

  // No placeholder entity. This used to render a synthesized all-zero record
  // during load *and on failure*, so a failed fetch showed a risk score of 0
  // in 34px type — a measurement the API never returned.
  if (!ent) {
    // A 404 is not a broken API — the account simply is not in this snapshot,
    // which is the ordinary outcome of following a link into a tenant that has
    // never seen it. Saying "could not load" there sends people looking for an
    // outage that is not happening.
    const missing = page.status === 404;
    return (
      <Page
        title={id || "Account"}
        source={page.source}
        error={page.error}
        notFound={missing}
        backTo="/users"
        onRefresh={page.reload}
      >
        <Pad>
          <Card>
            <EmptyState
              title={
                missing
                  ? "No such account in this snapshot"
                  : page.source === "error"
                    ? "Could not load this account"
                    : "Loading…"
              }
              hint={
                missing
                  ? `The current snapshot carries no findings for ${id}.`
                  : page.source === "error"
                    ? (page.error ?? "The API did not respond.")
                    : undefined
              }
            />
          </Card>
        </Pad>
      </Page>
    );
  }

  const sev = bandToSev(ent.band);
  const color = sevColor(sev, c);

  return (
    <Page
      title={ent.id}
      source={page.source}
      error={page.error}
      backTo="/users"
      onRefresh={page.reload}
    >
      <Pad>
        <Card className="flex flex-wrap items-center gap-5 p-5">
          <Avatar initials={ent.initials} color={ent.color} size={56} />
          <div className="min-w-0 flex-1">
            <div className="truncate font-mono text-[17px] font-bold text-ink">{ent.id}</div>
            <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-ink-3">
              {ent.address !== ent.id && <span className="font-mono">{ent.address}</span>}
              <span>
                {ent.devices ?? "—"} devices · {ent.ips ?? "—"} source IPs
              </span>
              {page.data?.anomalies != null && <span>{findingsLabel}</span>}
            </div>
          </div>
          <div className="flex items-center gap-4">
            <div className="text-right">
              {SHOW_SCORING && (
                <div className="font-mono text-[34px] font-bold leading-none" style={{ color }}>
                  {ent.score}
                </div>
              )}
              <div className="mt-1.5 flex items-center justify-end gap-2">
                <SevBadge sev={sev} />
                <span
                  className="font-mono text-[11px] font-semibold"
                  style={{ color: ent.delta > 0 ? c.crit : c.accent }}
                >
                  {ent.delta > 0 ? "+" : ""}
                  {ent.delta} / 24h
                </span>
              </div>
            </div>
            <Btn variant="primary" onClick={() => setTab("timeline")}>
              <I.Clock size={13} /> View Timeline
            </Btn>
          </div>
        </Card>

        <div className="flex gap-6 overflow-x-auto border-b border-line">
          {TABS.map((t) => (
            <button
              key={t.key}
              type="button"
              onClick={() => setTab(t.key)}
              className={`-mb-px shrink-0 cursor-pointer whitespace-nowrap border-b-2 pb-2.5 text-[13px] font-semibold transition-colors ${
                tab === t.key
                  ? "border-accent text-accent"
                  : "border-transparent text-ink-3 hover:text-ink-2"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>

        {tab === "overview" && (
          <div className="flex flex-col gap-4">
            <Card>
              <CardHead
                title={`${SHOW_SCORING ? "Score Contributions" : "Detections"} (${contributions.length})`}
                meta={findingsLabel}
              />
              {!severityBands ? (
                <EmptyState
                  title={SHOW_SCORING ? "Scoring parameters unavailable" : "Detection parameters unavailable"}
                  hint="Findings cannot be ranked without the pipeline's severity cutoffs."
                />
              ) : contributions.length === 0 ? (
                <EmptyState
                  title={SHOW_SCORING ? "No scored findings" : "No findings"}
                  hint={
                    ent.score > 0
                      ? SHOW_SCORING
                        ? `This account scores ${ent.score} over the pipeline's full scoring window, but has no findings in the last ${windowDays}d.`
                        : `This account has findings over the pipeline's full window, but none in the last ${windowDays}d.`
                      : `Nothing outside this account's learned baseline in the last ${windowDays}d.`
                  }
                />
              ) : (
                <div className="grid grid-cols-1 gap-x-6 gap-y-3.5 p-4 sm:grid-cols-2">
                  {contributions.slice(0, 8).map((g) => (
                    <div key={g.type}>
                      <div className="mb-1 flex items-center justify-between gap-3">
                        <span className="min-w-0 truncate font-mono text-[12px] font-semibold text-ink">
                          {g.type}
                        </span>
                        {g.count > 1 && (
                          <span
                            className="shrink-0 rounded px-1.5 py-0.5 font-mono text-[10.5px] font-bold"
                            style={{
                              color: sevColor(g.sev, c),
                              backgroundColor: `${sevColor(g.sev, c)}1f`,
                            }}
                            title={`Fired on ${g.count} separate days. Repeats of one detection saturate rather than adding up.`}
                          >
                            ×{g.count}
                          </span>
                        )}
                      </div>
                      {/* Bar length is the finding's share of the top
                          contribution, so it encodes the score even unpainted. */}
                      {SHOW_SCORING && (
                        <Bar pct={(g.points / maxPoints) * 100} color={sevColor(g.sev, c)} />
                      )}
                      <div className="mt-1 text-[11px] leading-snug text-ink-3">{g.detail}</div>
                    </div>
                  ))}
                  {contributions.length > 8 && (
                    <div className="text-[11px] text-ink-3 sm:col-span-2">
                      and {contributions.length - 8} more detection
                      {contributions.length - 8 === 1 ? "" : "s"}
                    </div>
                  )}
                </div>
              )}
            </Card>

            <LearnedBaseline profile={profile} />
          </div>
        )}

        {tab === "timeline" &&
          (severityBands ? (
            <ActivityTimeline timeline={rawTimeline} severityBands={severityBands} />
          ) : (
            <Card>
              <EmptyState
                title="Waiting for scoring parameters"
                hint="/api/tuning has not answered, so findings cannot be coloured by severity yet."
              />
            </Card>
          ))}

        {tab === "graph" &&
          (tuning ? (
            <GraphTab entity={ent.id} bands={tuning.bands} />
          ) : (
            <Card>
              <EmptyState
                title="Waiting for scoring parameters"
                hint="/api/tuning has not answered, so risk thresholds are unknown."
              />
            </Card>
          ))}
      </Pad>
    </Page>
  );
}

/** Index 0 is Monday — see `compact_profile` in the serving builder. */
const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/**
 * Histogram rows in slot order, not ranked. A weekday profile reads as a week —
 * frequency-sorting it hides the shape — and a zero day is a real observation
 * ("never signs in on a Sunday"), so empty slots stay in.
 */
function histRows(hist: number[] | undefined, labels: string[]) {
  const h = hist ?? [];
  if (h.length === 0) return [];
  const total = h.reduce((sum, n) => sum + n, 0) || 1;
  return labels.map((name, i) => ({
    name,
    n: h[i] ?? 0,
    pct: Math.round(((h[i] ?? 0) / total) * 100),
  }));
}

/** "Mumbai · Reliance Jio Infocomm Limited" from one address's enrichment. */
function geoLabel(g: IpGeo | undefined): string | undefined {
  if (!g) return undefined;
  return [g.city || g.country, g.asn].filter(Boolean).join(" · ") || undefined;
}

function Stat({ label, value }: { label: string; value: string | null }) {
  return (
    <div>
      <div className="text-[10px] font-semibold uppercase tracking-wide text-ink-3">{label}</div>
      <div className="mt-0.5 font-mono text-[13px] font-semibold text-ink">{value ?? "—"}</div>
    </div>
  );
}

/**
 * The baseline record as the detector wrote it — every counter, every value.
 * Nothing is sliced here: `RankPanel` scrolls instead, so an account with two
 * hundred addresses shows all two hundred rather than a silent top few.
 *
 * Every panel's empty text is now the same neutral "Not recorded." Each used to
 * assert a *reason* for the absence ("No geo data in the ingested feeds.", "No
 * failed sign-ins from any address.") — a claim about the pipeline that the
 * browser has no way to verify, and that is simply wrong when the counter is
 * missing for some other reason.
 */
function LearnedBaseline({ profile }: { profile: Profile }) {
  const ipGeo = profile.ip_geo ?? {};
  const n = (v: number | undefined) => (v == null ? null : v.toLocaleString());

  const panels: {
    title: string;
    rows: { name: string; n: number; pct: number }[];
    mono?: boolean;
    sub?: (name: string) => string | undefined;
  }[] = [
    { title: "Raw identities", rows: rank(profile.identifiers), mono: true },
    { title: "Countries", rows: rank(profile.countries) },
    { title: "Cities", rows: rank(profile.cities) },
    { title: "Devices", rows: rank(profile.devices), mono: true },
    { title: "ASNs", rows: rank(profile.asns), mono: true },
    { title: "Applications", rows: rank(profile.apps) },
    { title: "Client apps", rows: rank(profile.client_apps) },
    { title: "Auth requirements", rows: rank(profile.auth_requirements) },
    { title: "Source IPs", rows: rank(profile.ips), mono: true, sub: (ip) => geoLabel(ipGeo[ip]) },
    { title: "Hour of day (IST)", rows: histRows(profile.hours_hist, HOURS) },
    { title: "Day of week", rows: histRows(profile.dow_hist, DOW) },
    { title: "Failed source IPs", rows: rank(profile.failed_ips), mono: true, sub: (ip) => geoLabel(ipGeo[ip]) },
    { title: "Failed countries", rows: rank(profile.failed_countries) },
  ];

  return (
    <Card>
      <CardHead title="Learned Baseline" meta="Every counter written by the baseline builder" />
      <div className="flex flex-col gap-6 p-4">
        <div className="flex flex-wrap gap-x-7 gap-y-3">
          <Stat label="Window" value={profile.days_seen == null ? null : `${profile.days_seen}d`} />
          <Stat label="Logins" value={n(profile.login_count)} />
          <Stat label="Failed" value={n(profile.failed_login_count)} />
          <Stat label="Distinct IPs" value={n(profile.distinct_ips)} />
          <Stat label="Failed IPs" value={n(profile.distinct_failed_ips)} />
          <Stat label="First seen" value={dayLabel(profile.first_seen_ms)} />
          <Stat label="Last seen" value={dayLabel(profile.last_seen_ms)} />
        </div>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {panels.map((p) => (
            <RankPanel key={p.title} {...p} />
          ))}
        </div>
      </div>
    </Card>
  );
}

/** 24 slots. The baseline's `hour_histogram` is IST, per `compact_profile`. */
const HOURS = Array.from({ length: 24 }, (_, i) => `${String(i).padStart(2, "0")}:00`);

function RankPanel({
  title,
  rows,
  mono = false,
  sub,
}: {
  title: string;
  rows: { name: string; n: number; pct: number }[];
  mono?: boolean;
  /** Optional dimmed second line, e.g. an address's country and ASN. */
  sub?: (name: string) => string | undefined;
}) {
  const c = themeColors(useMode());
  return (
    <div className="rounded-lg border border-line bg-surface p-3">
      <div className="mb-2 flex items-baseline gap-1.5 border-b border-line pb-2">
        <span className="text-[12px] font-semibold text-ink">{title}</span>
        {rows.length > 0 && (
          <span className="font-mono text-[10.5px] text-ink-3">({rows.length})</span>
        )}
      </div>
      {rows.length === 0 ? (
        <div className="text-[11px] leading-snug text-ink-3">Not recorded.</div>
      ) : (
        // Capped height, not a capped row count: every value stays reachable
        // without a user's 200 addresses stretching the card off the page.
        <div className="max-h-64 space-y-2 overflow-y-auto pr-1">
          {rows.map((r) => {
            const detail = sub?.(r.name);
            return (
              <div key={r.name} className="flex items-center gap-2">
                <span className="min-w-0 flex-1">
                  <span
                    className={`block truncate text-[11.5px] text-ink-2 ${mono ? "font-mono" : ""}`}
                    title={r.name}
                  >
                    {r.name}
                  </span>
                  {detail && (
                    <span className="block truncate text-[10px] text-ink-3" title={detail}>
                      {detail}
                    </span>
                  )}
                </span>
                <span className="w-10 shrink-0 text-right font-mono text-[10.5px] text-ink-3">
                  {r.n.toLocaleString()}
                </span>
                <Bar pct={r.pct} color={c.blue} className="w-10 shrink-0" />
                <span className="w-8 shrink-0 text-right font-mono text-[10.5px] text-ink-3">
                  {r.pct}%
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function GraphTab({ entity, bands }: { entity: string; bands: ScoreBands }) {
  const nav = useNavigate();
  const [graph, setGraph] = useState<GraphData | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setGraph(null);
    setErr(null);
    api
      .graph(entity)
      .then(setGraph)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : String(e)));
  }, [entity]);

  return (
    <Card className="overflow-hidden">
      <CardHead title="Relationships" meta="Addresses, networks, countries and apps from the baseline" />
      {err ? (
        <EmptyState title="Graph unavailable" hint={err} />
      ) : !graph ? (
        <EmptyState title="Loading graph…" />
      ) : graph.nodes.length === 0 ? (
        <EmptyState
          title="No relationships recorded for this account"
          // The graph is built from the profile's own counters, not from a
          // graph store. There is no `ueba/serving/graph/<entity>.json`, which
          // is what this hint used to name.
          hint="Edges come from the baseline profile's address, network, country and application counters; this account has none."
        />
      ) : (
        <EntityGraph
          data={graph}
          height={460}
          bands={bands}
          onNodeClick={(id, type) => {
            if (type === "user") nav(`/users/${encodeURIComponent(id)}`);
          }}
        />
      )}
    </Card>
  );
}
