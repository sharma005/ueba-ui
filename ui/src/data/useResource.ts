import { useCallback, useEffect, useRef, useState } from "react";

export type Source = "loading" | "live" | "error";

/**
 * One API read.
 *
 * `data` is null until the request succeeds and **null again if it fails** —
 * this is the important part. It used to fall back to a caller-supplied
 * `initial` on error, and every caller's initial was a zeroed shape, so a dead
 * API rendered a complete, plausible dashboard of zeros: zero users, zero
 * anomalies, highest score 0, "LOW". Nothing on screen said the request had
 * failed except a small chip in the header.
 *
 * Callers must branch on null. `Page` already renders the source chip and the
 * error, so the usual handling is a "—" in place of the figure.
 */
export interface Resource<T> {
  data: T | null;
  source: Source;
  /** Message from the failed request, for the header chip's tooltip. */
  error: string | null;
  /**
   * HTTP status of the failed request, when it had one. Null on success and on
   * a transport failure that never reached the server.
   *
   * Lets callers tell "this thing is not in the snapshot" (404) from "the API
   * is down", which the header otherwise reports identically. Switching tenants
   * strands you on accounts the other customer has never heard of, so that
   * distinction stops being an edge case.
   */
  status: number | null;
  /**
   * Re-request. The promise settles when the request does, resolving with the
   * new value — or **null if it failed**, which is how a caller tells a failure
   * from a success, since this never rejects (the failure is already reported
   * through `source` and `error`).
   *
   * The header's refresh button needs both halves: the settle to stop spinning,
   * and the value to say whether a newer snapshot actually landed. Callers that
   * only want the side effect can ignore the result.
   */
  reload: () => Promise<T | null>;
}

export function useResource<T>(fetcher: () => Promise<T>, deps: unknown[] = []): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [source, setSource] = useState<Source>("loading");
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);
  const [nonce, setNonce] = useState(0);
  // Resolvers for `reload()` calls still waiting on the request they triggered.
  const waiting = useRef<((v: T | null) => void)[]>([]);

  useEffect(() => {
    // Scoped to this effect run, not shared across runs. It used to be a
    // `useRef` set true on entry and false in cleanup, which does not survive a
    // deps change: React runs the old cleanup and then the new effect, so the
    // flag is back to true by the time the *previous* request's `.then` reads
    // it, and the superseded response overwrites the current one.
    //
    // With a tenant in the deps that stops being a rare race and becomes a
    // routine cross-tenant leak — switch tenants, the slower previous request
    // lands second, and the screen shows one customer's accounts under the
    // other's name.
    let cancelled = false;
    setSource("loading");
    // Drained whether the request succeeded, failed, or was superseded — a
    // caller awaiting `reload()` must never be left hanging.
    const settle = (v: T | null) => {
      const pending = waiting.current;
      waiting.current = [];
      pending.forEach((resolve) => resolve(v));
    };
    fetcher()
      .then((v) => {
        const next = v ?? null;
        if (cancelled) return settle(next);
        setData(next);
        setSource("live");
        setError(null);
        setStatus(null);
        settle(next);
      })
      .catch((e: unknown) => {
        if (cancelled) return settle(null);
        const code = (e as { status?: unknown } | null)?.status;
        setData(null);
        setSource("error");
        setError(e instanceof Error ? e.message : String(e));
        setStatus(typeof code === "number" ? code : null);
        settle(null);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  const reload = useCallback(
    () =>
      new Promise<T | null>((resolve) => {
        waiting.current.push(resolve);
        setNonce((n) => n + 1);
      }),
    [],
  );
  return { data, source, error, status, reload };
}

/** Worst of several sources — a page is broken if any panel on it failed. */
export function worstSource(...s: Source[]): Source {
  if (s.includes("error")) return "error";
  if (s.includes("loading")) return "loading";
  return "live";
}
