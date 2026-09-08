import { useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { Pad, Page } from "../components/Page";
import { Avatar, Card, EmptyState, Select } from "../components/primitives";
import { useTenant, useTuning } from "../components/Shell";
import { fromIndexEntity } from "../data/adapt";
import { useResource } from "../data/useResource";
import { SHOW_SCORING, type ScoreBands } from "../theme";

/**
 * Risk-floor options, built from the bands the pipeline actually scores with.
 *
 * This used to be the literal list ["0","45","70","85"] — mirroring cutoffs
 * the UI had invented, one of which (85) did not exist anywhere in the
 * pipeline. There is no entity-kind filter any more either: the backend
 * reports only `entity_type: "user"` here, and "Service" was a UI guess from
 * an `svc-`/`rpa-` name prefix.
 */
/** Shows the risk-floor dropdown. Off hides the control and leaves the filter a no-op. */
const SHOW_SCORE_FILTER = true;

function scoreFloors(bands: ScoreBands): { value: string; label: string }[] {
  return [
    { value: "0", label: "All scores" },
    { value: String(bands.medium), label: `Risk ≥ ${bands.medium} (medium)` },
    { value: String(bands.high), label: `Risk ≥ ${bands.high} (high)` },
    { value: String(bands.notable), label: `Risk ≥ ${bands.notable} (notable)` },
  ];
}

export default function Users() {
  const nav = useNavigate();
  const { tuning } = useTuning();
  const { tenant } = useTenant();
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const [minScore, setMinScore] = useState(0);

  const rows = useResource(
    () => api.entities(q).then((r) => (r.entities ?? []).map(fromIndexEntity)),
    [q, tenant],
  );

  const visible = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (rows.data ?? [])
      .filter((e) => e.score >= minScore)
      // Matches on the identifier, which is the only name this data has.
      .filter((e) => !needle || e.id.toLowerCase().includes(needle))
      .sort((a, b) => b.score - a.score);
  }, [rows.data, minScore, q]);

  const floors = tuning ? scoreFloors(tuning.bands) : null;
  const total = rows.data?.length ?? 0;
  const hidden = total - visible.length;

  return (
    <Page
      title="Accounts"
      subtitle={
        tuning
          ? SHOW_SCORING
            ? `Ranked by risk score over ${tuning.baselineDays} days with a ${tuning.decayDays}-day half-life`
            : `Findings over a ${tuning.baselineDays}-day baseline window`
          : undefined
      }
      source={rows.source}
      error={rows.error}
      backTo="/"
      onRefresh={rows.reload}
      noWindow
      actions={
        SHOW_SCORE_FILTER && floors ? (
          <div className="flex flex-1 items-center justify-end gap-2">
            <Select
              value={String(minScore)}
              onChange={(v) => setMinScore(Number(v))}
              options={floors.map((f) => f.value)}
              format={(v) => floors.find((f) => f.value === v)?.label ?? v}
              label="Minimum score"
            />
          </div>
        ) : undefined
      }
    >
      <Pad>
        {rows.data != null && visible.length > 0 && (
          <div className="text-[12px] text-ink-3">
            {visible.length} {visible.length === 1 ? "account" : "accounts"}
            {hidden > 0 && <span className="opacity-70"> · {hidden} hidden by filter</span>}
          </div>
        )}

        {q && (
          <div className="flex items-center gap-2 text-[12px] text-ink-3">
            Filtered by <span className="font-mono text-ink-2">{q}</span>
            <button
              type="button"
              onClick={() => setParams({})}
              className="cursor-pointer text-accent hover:underline"
            >
              clear
            </button>
          </div>
        )}

        {rows.data == null ? (
          <Card>
            <EmptyState
              title={rows.source === "error" ? "Could not load accounts" : "Loading…"}
              hint={rows.error ?? undefined}
            />
          </Card>
        ) : visible.length === 0 ? (
          <Card>
            <EmptyState
              title="No accounts match this filter"
              hint={
                SHOW_SCORE_FILTER
                  ? "Loosen the risk floor. A freshly deployed pipeline has no scored accounts until the first build completes."
                  : "A freshly deployed pipeline has no scored accounts until the first build completes."
              }
            />
          </Card>
        ) : (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4">
            {visible.map((e) => (
              <Card
                key={e.id}
                className="cursor-pointer overflow-hidden transition-colors hover:bg-card-hover"
              >
                <button
                  type="button"
                  onClick={() => nav(`/users/${encodeURIComponent(e.id)}`)}
                  className="w-full cursor-pointer text-left"
                >
                  <div className="flex items-center gap-3 p-4">
                    <Avatar initials={e.initials} color={e.color} size={40} />
                    <div className="min-w-0 flex-1">
                      <div className="truncate font-mono text-[13px] font-semibold text-ink" title={e.id}>
                        {e.id}
                      </div>
                      <div className="truncate text-[11.5px] text-ink-3">
                        {e.last ? `last finding ${e.last}` : "no findings recorded"}
                      </div>
                    </div>
                  </div>
                </button>
              </Card>
            ))}
          </div>
        )}
      </Pad>
    </Page>
  );
}
