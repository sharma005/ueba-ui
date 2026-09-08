import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { matchPath, Outlet, useLocation, useNavigate } from "react-router-dom";
import { api, setApiTenant } from "../api";
import { useResource } from "../data/useResource";
import { useCanGoBackTrail } from "../nav";
import { applyMode, currentMode, type Mode, type ScoreBands, type SeverityBands } from "../theme";
import type { Health, ScoringParams } from "../types";
import { AdminPanel } from "./AdminPanel";
import { Sidebar } from "./Sidebar";

/**
 * The pipeline's own scoring parameters, from `/api/tuning`.
 *
 * Null until the API answers, and null forever if it fails — deliberately.
 * There used to be a `TUNING_PENDING` here holding 90 / 7d / 30d, which the
 * screens printed as fact ("Ranked by risk score with 7-day decay applied")
 * before the request landed and kept printing if it never did. Every consumer
 * now branches on null and renders "—" instead.
 */
export interface Tuning {
  bands: ScoreBands;
  severityBands: SeverityBands;
  decayDays: number;
  baselineDays: number;
}

export const WINDOWS: Record<string, number> = {
  "Last 24h": 1,
  "Last 7 days": 7,
  "Last 30 days": 30,
};

const WINDOW_KEY = "cx-ueba-window";
const DEFAULT_WINDOW = "Last 7 days";

/**
 * The customers this console serves, from `/api/tenants`.
 *
 * Server-driven rather than a literal here: the serving layer's registry is the
 * thing that actually knows which buckets exist and whether each has a snapshot
 * yet, so onboarding a customer is one edit in `serving/tenants.py` and needs no
 * UI release.
 */
export interface TenantInfo {
  /** Wire identity, sent as `?tenant=`. Matches the detector's `TENANT_ID`. */
  id: string;
  label: string;
  /** The identity provider the findings come from, shown under the picker. */
  product: string;
  /** What the findings are built from, e.g. "Okta System Log". */
  source?: string;
  /**
   * Whether a snapshot exists for this customer. False is a real, ordinary
   * state — a detector can be running while the builder has never produced one
   * — so the picker keeps such a tenant selectable and badges it rather than
   * hiding the very condition someone needs to act on.
   */
  ready?: boolean;
  /** Why the tenant could not be read at all, when that is the reason. */
  error?: string;
}

const TENANT_KEY = "cx-ueba-tenant";
const DEFAULT_TENANT = "jiostar";

/**
 * What to show before `/api/tenants` answers, and if it fails.
 *
 * One entry, the default: enough for the picker to render a stable label on the
 * first paint without inventing customers that may not exist. The control hides
 * itself below two tenants, so a failed list degrades to no picker rather than
 * to a wrong one.
 */
const FALLBACK_TENANTS: TenantInfo[] = [
  { id: DEFAULT_TENANT, label: "JioStar", product: "Microsoft Entra ID" },
];

interface ShellCtx {
  /** Null until `/api/tuning` answers, and null if it failed. */
  tuning: Tuning | null;
  /**
   * `/api/health`, for the one thing every header needs: when the snapshot the
   * API is serving was actually built. Null until it answers, and null if it
   * failed — the header says "Snapshot unknown" rather than inventing an age.
   */
  health: Health | null;
  /** Re-request `/api/health`, resolving with the fresh response — or null if
   *  it failed. The refresh button reads both to report the outcome. */
  reloadHealth: () => Promise<Health | null>;
  /**
   * Why `tuning` is null, when the reason is a failure rather than a pending
   * request. Every screen surfaces this: without the pipeline's cutoffs the
   * console cannot colour a severity or name a band, and it must say so rather
   * than rendering as though there were simply nothing to show.
   */
  tuningError: string | null;
  /** Re-request `/api/tuning`. It is fetched once at mount, so a failure at
   *  that moment would otherwise degrade the whole app until a full reload. */
  reloadTuning: () => Promise<unknown>;
  mode: Mode;
  window: string;
  windowDays: number;
  setWindow: (w: string) => void;
  /** The selected customer's id. Every request carries it. */
  tenant: string;
  /** The same customer's display record, for labels and captions. */
  current: TenantInfo;
  /** Everyone the API says it serves, in registry order. */
  tenants: TenantInfo[];
  setTenant: (id: string) => void;
  /** Whether there is a screen behind this one inside the app, so `Page`'s
   *  Back can step through history instead of jumping to a fixed route. */
  canGoBack: boolean;
}

const Ctx = createContext<ShellCtx>({
  tuning: null,
  health: null,
  reloadHealth: async () => null,
  tuningError: null,
  reloadTuning: async () => {},
  mode: "dark",
  window: DEFAULT_WINDOW,
  windowDays: WINDOWS[DEFAULT_WINDOW],
  setWindow: () => {},
  tenant: DEFAULT_TENANT,
  current: FALLBACK_TENANTS[0],
  tenants: FALLBACK_TENANTS,
  setTenant: () => {},
  canGoBack: false,
});

export const useTuning = () => {
  const { tuning, tuningError, reloadTuning } = useContext(Ctx);
  return { tuning, tuningError, reloadTuning };
};

/** How fresh the data on screen is, and how to re-check. */
export const useHealth = () => {
  const { health, reloadHealth } = useContext(Ctx);
  return { health, reloadHealth };
};

/**
 * The shared time window. Every section reads it; the header sets it.
 *
 * `since` is the floor to filter records by, and it is anchored to the
 * snapshot's own clock rather than the browser's. The pipeline counted
 * `n_anomalies` from the instant it built; a browser counting from `Date.now()`
 * uses a floor up to one rebuild cycle (2h) later and silently drops whatever
 * falls in between — the roster card said 27 findings while the entity page
 * said 26. Anchoring here makes the two agree by construction, and keeps the
 * next page from re-deriving its own floor.
 *
 * Falls back to the browser clock only before the first snapshot has landed,
 * when there are no server counts to disagree with anyway.
 */
export const useWindow = () => {
  const { window, windowDays, setWindow, health } = useContext(Ctx);
  const anchor = health?.snapshot_generated_at_ms ?? Date.now();
  return { window, windowDays, setWindow, since: anchor - windowDays * 86_400_000 };
};

/**
 * The customer on screen. Every page puts `tenant` in its `useResource` deps so
 * a switch refetches — a page that forgets keeps showing the previous
 * customer's data, and nothing in the type system or the linter will say so.
 */
export const useTenant = () => {
  const { tenant, current, tenants, setTenant } = useContext(Ctx);
  return { tenant, current, tenants, setTenant };
};

/** Charts read literal colours, so they re-render when this flips. */
export const useMode = () => useContext(Ctx).mode;

/** True once this tab has visited more than one screen. `Page` uses it to send
 *  Back through history rather than to its declared fallback. */
export const useCanGoBack = () => useContext(Ctx).canGoBack;

/**
 * `/api/tuning` -> `Tuning`, or null if the response is missing any cutoff.
 *
 * All-or-nothing on purpose: a partial ladder would have to be completed with
 * invented numbers, which is the thing this console is not allowed to do.
 */
function toTuning(p: ScoringParams | null): Tuning | null {
  const s = p?.scoring;
  const b = s?.bands;
  const sb = s?.severity_bands;
  if (
    b?.notable == null || b.high == null || b.medium == null ||
    sb?.critical == null || sb.error == null || sb.warning == null ||
    s?.half_life_days == null || s.window_days == null
  ) {
    return null;
  }
  return {
    bands: { notable: b.notable, high: b.high, medium: b.medium },
    severityBands: { critical: sb.critical, error: sb.error, warning: sb.warning },
    decayDays: s.half_life_days,
    baselineDays: s.window_days,
  };
}

export function Shell() {
  const nav = useNavigate();
  const { pathname } = useLocation();
  // Here, not in `Page`: every page unmounts on a route change, so a trail kept
  // in one would be a single entry long and never know where Back leads.
  const canGoBack = useCanGoBackTrail();

  // Hydrated synchronously, exactly as the window below is. It has to be: the
  // four requests this component fires at mount must go out under the right
  // customer on the first render. Reading it in an effect instead would load
  // JioStar's several-megabyte dashboard and then re-request, flashing the
  // wrong customer's numbers on the way.
  const [tenant, setTen] = useState<string>(() => {
    try {
      const saved = localStorage.getItem(TENANT_KEY);
      // Not validated against a list here — there is none yet on the first
      // render. `/api/tenants` validates it when it lands, below.
      return saved || DEFAULT_TENANT;
    } catch {
      return DEFAULT_TENANT;
    }
  });

  // In the render body, not an effect. React renders `Shell` before any
  // descendant's effect runs and `Shell` is the `Outlet` parent of every page,
  // so this is set before any page builds a fetcher closure. From an effect,
  // a page's first request would go out under the previous customer.
  setApiTenant(tenant);

  const setTenant = useCallback((id: string) => {
    setTen(id);
    try {
      localStorage.setItem(TENANT_KEY, id);
    } catch {
      // Private browsing and the like. The selection still applies to this
      // session; it just will not survive a reload.
    }
    // An account id is only meaningful within one customer, so staying on
    // `/users/someone@jiostar.com` after a switch resolves to a 404. Every
    // other route is valid for any customer, so leave it alone — dropping
    // someone back to the dashboard from `/anomalies` loses their place for
    // no reason.
    if (matchPath("/users/:id", pathname)) nav("/users", { replace: true });
  }, [nav, pathname]);

  // Tenant-free deps: the registry does not change when the selection does, and
  // re-requesting it on every switch would be a wasted round trip.
  const tenantIndex = useResource(() => api.tenants(), []);
  const tenantList = useMemo<TenantInfo[]>(
    () => (tenantIndex.data?.tenants?.length
      ? tenantIndex.data.tenants
      : FALLBACK_TENANTS),
    [tenantIndex.data],
  );

  // A stored id the API does not recognise would make every request 400, so
  // correct it as soon as the real list lands. Only once it has actually
  // answered — an unreachable API must not silently reset the selection.
  useEffect(() => {
    const known = tenantIndex.data?.tenants;
    if (!known?.length) return;
    if (!known.some((t) => t.id === tenant)) {
      setTenant(tenantIndex.data!.default || known[0].id);
    }
  }, [tenantIndex.data, tenant, setTenant]);

  const serverTuning = useResource(() => api.tuning(), [tenant]);
  const health = useResource(() => api.health(), [tenant]);
  const tuning = useMemo(() => toTuning(serverTuning.data), [serverTuning.data]);
  const [mode, setMode] = useState<Mode>(() => currentMode());
  const [adminOpen, setAdminOpen] = useState(false);
  const [win, setWin] = useState<string>(() => {
    try {
      const saved = localStorage.getItem(WINDOW_KEY);
      return saved && saved in WINDOWS ? saved : DEFAULT_WINDOW;
    } catch {
      return DEFAULT_WINDOW;
    }
  });

  const setWindow = useCallback((w: string) => {
    setWin(w);
    try {
      localStorage.setItem(WINDOW_KEY, w);
    } catch {
    }
  }, []);
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem("cx-ueba-sidebar") === "collapsed";
    } catch {
      return false;
    }
  });

  const [narrow, setNarrow] = useState(() => window.innerWidth < 1024);
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 1023px)");
    const onChange = (e: MediaQueryListEvent | MediaQueryList) => setNarrow(e.matches);
    onChange(mq);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  const setModeExplicit = useCallback((m: Mode) => {
    applyMode(m);
    setMode(m);
  }, []);

  const toggleCollapse = useCallback(() => {
    setCollapsed((c) => {
      const next = !c;
      try {
        localStorage.setItem("cx-ueba-sidebar", next ? "collapsed" : "expanded");
      } catch {
        // ignore
      }
      return next;
    });
  }, []);

  // Every badge counts over the window the header selected, so the number
  // beside a section matches what that section will show. It used to be pinned
  // to 24h and disagreed with its own page.
  //
  // `limit: "1"` because only the count is wanted: `/anomalies` reports the
  // true `total` for the window separately from the rows it returns, so asking
  // for 1000 rows and taking `.length` silently capped the badge at 1000.
  const windowDays = WINDOWS[win];
  const anomalyCount = useResource(
    () =>
      api.anomalies({ days: String(windowDays), limit: "1" }).then((r) => ({
        total: r.total,
        estimate: r.total_is_estimate ?? false,
      })),
    [windowDays, tenant],
  );
  // The alert badge counts accounts in the notable band, which is what the
  // Alerts page lists. It reads the builder's own band tally rather than
  // filtering `/notables`: that document is the top-`NOTABLE_LIMIT` shortlist
  // (50), so the badge saturated at 50 while the Dashboard's Alerts tile —
  // reading this same KPI — showed the true 510.
  const alertCount = useResource(
    () => api.overview().then((r) => r.kpis?.notable_entities ?? null),
    [tenant],
  );
  const counts = useMemo(
    () => ({
      anomalies: anomalyCount.data?.total ?? null,
      alerts: alertCount.data ?? null,
    }),
    [anomalyCount.data, alertCount.data],
  );

  const current = useMemo(
    () => tenantList.find((t) => t.id === tenant) ?? tenantList[0],
    [tenantList, tenant],
  );
  const live = anomalyCount.source === "live";

  return (
    <Ctx.Provider
      value={{
        tuning,
        health: health.data,
        reloadHealth: health.reload,
        // A successful response that is missing a cutoff is also a failure:
        // `toTuning` refuses to complete a partial ladder.
        tuningError:
          serverTuning.source === "error"
            ? (serverTuning.error ?? "/api/tuning did not respond")
            : serverTuning.source === "live" && tuning === null
              ? "/api/tuning answered without the scoring cutoffs"
              : null,
        reloadTuning: serverTuning.reload,
        mode, window: win, windowDays, setWindow,
        tenant, current, tenants: tenantList, setTenant,
        canGoBack,
      }}
    >
      <div className="flex h-screen overflow-hidden bg-bg text-ink">
        <Sidebar
          counts={counts}
          collapsed={collapsed || narrow}
          onToggleCollapse={toggleCollapse}
          mode={mode}
          onSetMode={setModeExplicit}
          ingest={{
            label: live ? "Pipeline live" : anomalyCount.source === "loading" ? "Connecting" : "API unreachable",
            detail: live
              // The window the badge actually counted, and whether `/anomalies`
              // flagged the total as an estimate (it does for a window wider
              // than the stored feed).
              ? `${anomalyCount.data?.estimate ? "~" : ""}${anomalyCount.data?.total ?? 0}` +
                ` anomalies in ${win.toLowerCase()}`
              : anomalyCount.source === "loading"
                ? "Contacting the API…"
                : (anomalyCount.error ?? "No response from the API"),
            ok: live,
          }}
        />
        <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
          {/* The API now resolves the tenant per request, so what is on screen
              really is this customer's. What can still be absent is their
              snapshot — the detector may be running while the builder has never
              produced one — and that is worth saying once here rather than
              leaving five pages to render as though the customer were simply
              quiet. */}
          {current?.ready === false && (
            <div className="shrink-0 border-b border-[var(--sev-medium-border)] bg-[var(--sev-medium-bg)] px-4 py-2.5 text-[12px] leading-relaxed text-med sm:px-6">
              <span className="font-semibold">No snapshot for {current.label} yet.</span>{" "}
              {current.error
                ? `The console could not read this tenant's state: ${current.error}`
                : "Nothing on these screens is missing — there is genuinely nothing " +
                  "built for this customer to show."}
            </div>
          )}
          {/* A half-learned baseline is the one failure mode this console cannot
              show by rendering the data honestly: the detector suppresses
              findings for accounts it has not seen yet, so an incomplete
              baseline looks exactly like a quiet customer. It has to be stated.
              Note this is the baseline's own record of itself, not the run
              telemetry — the last run can report "ok" while the baseline it
              wrote still covers only part of the history. */}
          {health.data?.baseline_incomplete && current?.ready !== false && (
            <div className="shrink-0 border-b border-[var(--sev-medium-border)] bg-[var(--sev-medium-bg)] px-4 py-2.5 text-[12px] leading-relaxed text-med sm:px-6">
              <span className="font-semibold">
                {current?.label ?? "This tenant"}&rsquo;s baseline is still being learned.
              </span>{" "}
              The detector has not finished reading this customer&rsquo;s history
              {health.data.baseline_resume_before
                ? ` (still filling in before ${health.data.baseline_resume_before.slice(0, 10)})`
                : ""}
              , and it suppresses findings for accounts it has not seen yet. Counts here
              are a floor, not a total &mdash; treat a quiet account as unknown rather
              than clean.
            </div>
          )}
          <Outlet />
        </div>
        <AdminPanel
          open={adminOpen}
          onClose={() => setAdminOpen(false)}
          tuning={serverTuning.data}
          health={health.data}
          tenant={current}
        />
      </div>
    </Ctx.Provider>
  );
}
