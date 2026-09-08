import type {
  Anomaly, EntityPage, GraphData, Health, IndexEntity, NotableEntity,
  Overview, ScoringParams,
} from "./types";

const BASE = import.meta.env.VITE_API_BASE ?? "";
const API_KEY = import.meta.env.VITE_API_KEY ?? "";

/**
 * Cache key for the current session's reads, bumped by the refresh button.
 *
 * Deployed, `/api/*` sits behind a CloudFront policy that holds responses for
 * 300s, so re-requesting an identical URL inside that window is answered at the
 * edge and the console shows the same payload it already had. The policy keys on
 * the whole query string (`QueryStringBehavior: all`), so a changed `_r` is a
 * different cache key and is guaranteed to reach the origin — which HEADs S3 and
 * serves whatever snapshot is current.
 *
 * Zero until the first manual refresh: ordinary navigation should keep hitting
 * the cache, because a dashboard load is several megabytes.
 */
let epoch = 0;

/** Skip the edge cache on every subsequent read. Called by the refresh button. */
export const bustCache = () => {
  epoch = Date.now();
};

/**
 * The customer whose data every read is for, sent as `?tenant=`.
 *
 * Module-level rather than an argument on all ten endpoints: `get` below is
 * already the one place that appends `_r=` and attaches the API key, and a
 * `tenant` parameter threaded through positional signatures like
 * `entities(q, band, type)` would buy no correctness the `useResource` deps
 * arrays don't already provide.
 *
 * `Shell` writes this from its render body, not an effect. React renders
 * `Shell` before any descendant's effect runs, and `Shell` is the `Outlet`
 * parent of every page, so the value is correct before any child builds a
 * fetcher closure. Setting it from an effect would let a page's first request
 * go out under the previous tenant.
 *
 * Deployed, the CloudFront policy for `/api/*` keys on the whole query string,
 * so two tenants can never share a cached response.
 */
let tenant = "";

export const setApiTenant = (id: string) => {
  tenant = id;
};

async function request<T>(path: string, scoped: boolean): Promise<T> {
  const params: string[] = [];
  if (scoped && tenant) params.push(`tenant=${encodeURIComponent(tenant)}`);
  if (epoch) params.push(`_r=${epoch}`);
  const q = params.length ? `${path.includes("?") ? "&" : "?"}${params.join("&")}` : "";
  const r = await fetch(`${BASE}/api${path}${q}`, {
    // The browser's own HTTP cache, which the query string alone would not defeat
    // on a back/forward navigation.
    cache: "no-store",
    headers: API_KEY ? { "X-Api-Key": API_KEY } : {},
  });
  if (!r.ok) throw new ApiError(r.status, await r.text());
  return r.json();
}

/** A read for the selected customer. Everything but `/tenants`. */
const get = <T,>(path: string) => request<T>(path, true);

/** A read that is not about any one customer. Only `/tenants`. */
const getUnscoped = <T,>(path: string) => request<T>(path, false);

/**
 * A failed read, carrying the status so callers can tell "this thing is not in
 * the snapshot" from "the API is down".
 *
 * Both used to arrive as a bare `Error`, so a 404 on an entity page rendered
 * the red "API unreachable" chip — wrong even before tenants, and about to
 * become the ordinary case, since switching customers strands you on an
 * account the other one has never heard of.
 */
export class ApiError extends Error {
  constructor(public readonly status: number, body: string) {
    super(`${status} ${body}`);
    this.name = "ApiError";
  }

  get notFound() {
    return this.status === 404;
  }
}

export const api = {
  health: () => get<Health>("/health"),
  overview: () => get<Overview>("/overview"),
  notables: () => get<{ entities: NotableEntity[] }>("/notables"),
  entities: (q: string, band?: string, type?: string) =>
    get<{ entities: IndexEntity[]; total: number }>(
      `/entities?q=${encodeURIComponent(q)}${band ? `&band=${band}` : ""}${type ? `&type=${type}` : ""}`,
    ),
  entity: (id: string) => get<EntityPage>(`/entities/${encodeURIComponent(id)}`),
  graph: (id: string) => get<GraphData>(`/entities/${encodeURIComponent(id)}/graph`),
  events: (id: string, hours = 48) =>
    get<{ events: Record<string, any>[] }>(`/entities/${encodeURIComponent(id)}/events?hours=${hours}`),
  // `total` is the true count for the window, independent of how many rows
  // `limit` returned; `truncated` says rows were cut, and `total_is_estimate`
  // that the window reached past the stored feed and the total came from the
  // builder's per-day tally. See `routes.anomalies`.
  anomalies: (params: Record<string, string>) =>
    get<{
      anomalies: Anomaly[];
      total: number;
      truncated?: boolean;
      total_is_estimate?: boolean;
    }>(`/anomalies?${new URLSearchParams(params)}`),
  tuning: () => get<ScoringParams>("/tuning"),
  // Deliberately not carrying `?tenant=` — this is the route that answers
  // *about* tenants rather than for one, and the API rejects a tenant it does
  // not know, so sending a stale stored id here would break the very request
  // that would have corrected it.
  tenants: () => getUnscoped<TenantsResponse>("/tenants"),
};

export interface TenantsResponse {
  default: string;
  tenants: {
    id: string;
    label: string;
    product: string;
    source?: string;
    ready?: boolean;
    error?: string;
    snapshot_generated_at?: string | null;
    entities?: number | null;
    anomalies_30d?: number | null;
  }[];
}

export const fmtTs = (ts: number) =>
  new Date(ts).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });

