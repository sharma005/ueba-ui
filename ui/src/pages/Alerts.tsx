import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Pad, Page } from "../components/Page";
import { Avatar, Bar, Card, CardHead, EmptyState, SevBadge } from "../components/primitives";
import { useMode, useTenant, useTuning } from "../components/Shell";
import { fromIndexEntity } from "../data/adapt";
import type { EntityVM } from "../data/models";
import { useResource } from "../data/useResource";
import { bandToSev, sevColor, themeColors } from "../theme";

/**
 * Accounts the pipeline scored into the notable band.
 *
 * There is no alert store behind this: the detector emits findings, the builder
 * scores entities, and nothing in the pipeline raises or acknowledges an alert.
 * `alerts_today` used to sit in the overview KPIs and no longer does — the
 * current builder never writes it. So "alert" here means exactly one thing, and
 * the page says so rather than implying a triage queue that does not exist:
 * an account at or above `bands.notable`.
 *
 * The band is the server's own — rows come back band-filtered — and the cutoff
 * quoted in the subtitle comes from `/api/tuning` for the same reason `levelOf`
 * refuses a default: a copy of 90 here is how the UI and the pipeline drift
 * apart.
 */
export default function Alerts() {
  const { tuning } = useTuning();
  const { tenant } = useTenant();
  const c = themeColors(useMode());

  // `/entities?band=notable` rather than `/notables`: the latter is the
  // builder's top-`NOTABLE_LIMIT` (50) shortlist, so this page listed 50 rows
  // and said "50 alerting" on an estate with 510 alerting accounts. The band
  // filter runs over the full ranked index, and `total` is the true count
  // however many rows the server's own limit returned.
  const alerts = useResource(
    () =>
      api.entities("", "notable").then((r) => ({
        // Already ranked by score in the index. `n_anomalies_24h`, not the
        // window count `fromIndexEntity` picks, because the row says "in 24h".
        rows: r.entities.map((e) => ({ ...fromIndexEntity(e), anomalies: e.n_anomalies_24h })),
        total: r.total,
      })),
    [tenant],
  );

  // Only for the subtitle now — the band comes from the server, so no
  // threshold comparison happens here at all.
  const floor = tuning?.bands.notable;
  const rows = alerts.data?.rows ?? null;

  return (
    <Page
      title="Alerts"
      subtitle={
        floor == null
          ? undefined
          : `Accounts scoring ${floor} or above — the pipeline's notable band`
      }
      source={alerts.source}
      error={alerts.error}
      backTo="/"
      onRefresh={alerts.reload}
      noWindow
    >
      <Pad>
        <Card className="flex flex-col">
          <CardHead
            title="Notable accounts"
            meta={
              alerts.data == null
                ? undefined
                : alerts.data.total > rows!.length
                  ? `${alerts.data.total} alerting · top ${rows!.length} shown`
                  : `${alerts.data.total} alerting`
            }
          />
          {rows == null ? (
            <EmptyState
              title={alerts.source === "error" ? "Could not load alerts" : "Loading…"}
              hint={alerts.error ?? undefined}
            />
          ) : rows.length === 0 ? (
            <EmptyState
              title="Nothing alerting"
              hint={`No account is in the notable band${floor == null ? "" : ` (${floor} or above)`} over the pipeline's scoring window. This is a quiet estate, not a failed request.`}
            />
          ) : (
            rows.map((e) => <AlertRow key={e.id} e={e} c={c} />)
          )}
        </Card>
      </Pad>
    </Page>
  );
}

/** One alerting account. Mirrors `NotableRow` on the dashboard. */
function AlertRow({ e, c }: { e: EntityVM; c: ReturnType<typeof themeColors> }) {
  const nav = useNavigate();
  // The band the pipeline assigned, never a threshold comparison done here.
  const sev = bandToSev(e.band);
  const color = sevColor(sev, c);

  return (
    <button
      type="button"
      onClick={() => nav(`/users/${encodeURIComponent(e.id)}`)}
      className="flex w-full cursor-pointer items-center gap-3 border-b border-line-2 px-4 py-3 text-left transition-colors last:border-b-0 hover:bg-card-hover"
    >
      <Avatar initials={e.initials} color={e.color} size={32} />
      <span className="min-w-0 flex-1">
        <span className="block truncate font-mono text-[12.5px] font-semibold text-ink">{e.id}</span>
        <span className="block truncate text-[11px] text-ink-3">
          {e.anomalies == null
            ? "no finding count"
            : `${e.anomalies} finding${e.anomalies === 1 ? "" : "s"} in 24h`}
        </span>
      </span>
      <span
        className="shrink-0 font-mono text-[11px] font-semibold"
        style={{ color: e.delta > 0 ? c.crit : c.accent }}
      >
        {e.delta > 0 ? "+" : ""}
        {e.delta} / 24h
      </span>
      <Bar pct={e.score} color={color} className="w-14 shrink-0" />
      <span className="w-7 shrink-0 text-right font-mono text-[13px] font-bold" style={{ color }}>
        {e.score}
      </span>
      <SevBadge sev={sev} />
    </button>
  );
}
