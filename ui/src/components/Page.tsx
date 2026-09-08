import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { bustCache } from "../api";
import type { Source } from "../data/useResource";
import { Btn, Notice, Select } from "./primitives";
import { useCanGoBack, useHealth, useTenant, useTuning, useWindow, WINDOWS } from "./Shell";
import * as I from "./icons";
import { SHOW_SCORING } from "../theme";

export function Page({
  title,
  subtitle,
  backTo,
  onRefresh,
  actions,
  noWindow,
  children,
}: {
  title: string;
  subtitle?: string;
  source: Source;
  /** Surfaced in the error chip's tooltip. */
  error?: string | null;
  /**
   * The request failed because the thing does not exist, not because the API
   * is down. The chip says so instead of crying "API unreachable" over what is
   * really an empty result.
   */
  notFound?: boolean;
  /**
   * Give this screen a Back button. Back steps through history, so this value
   * is only the fallback for a cold entry — a deep link or a reload — where
   * there is no previous screen in this tab to return to.
   */
  backTo?: string;
  /** Re-request this page's data. Return the promise and the button spins until
   *  the request settles. */
  onRefresh?: () => void | Promise<unknown>;
  /** Filter dropdowns, search, etc. — rendered between the title and status. */
  actions?: ReactNode;
  /** Opt out of the shared time window on sections with no time dimension. */
  noWindow?: boolean;
  children: ReactNode;
}) {
  const nav = useNavigate();
  const canGoBack = useCanGoBack();
  const { window: win, setWindow } = useWindow();
  const { tenant, tenants, setTenant } = useTenant();
  const { tuningError, reloadTuning } = useTuning();
  const { health, reloadHealth } = useHealth();
  const [busy, setBusy] = useState(false);
  // Transient outcome of the last click, cleared by a timer.
  const [note, setNote] = useState<string | null>(null);
  const noteTimer = useRef<number | undefined>(undefined);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      clearTimeout(noteTimer.current);
    };
  }, []);

  const flash = useCallback((message: string) => {
    if (!mounted.current) return;
    setNote(message);
    clearTimeout(noteTimer.current);
    noteTimer.current = setTimeout(() => {
      if (mounted.current) setNote(null);
    }, 3000) as unknown as number;
  }, []);

  const refresh = useCallback(async () => {
    if (busy) return;
    setBusy(true);
    setNote(null);
    // What the API was serving before the round-trip, so the outcome can say
    // whether a newer snapshot actually landed rather than just that the
    // request succeeded.
    const before = health?.snapshot_generated_at ?? null;
    // Every read from here on carries a fresh cache key, so this goes past
    // CloudFront to the origin and the origin re-checks S3.
    bustCache();
    // The outcome is read off the health response itself rather than off a
    // rejection: `reload` reports failure by resolving null, so a dead API
    // would otherwise look like a successful "already current".
    const [, next] = await Promise.all([onRefresh?.(), reloadHealth()]);
    if (!mounted.current) return;
    flash(
      next == null
        ? "Refresh failed"
        : next.snapshot_generated_at && next.snapshot_generated_at === before
          ? "Already current"
          : "Updated",
    );
    setBusy(false);
  }, [busy, flash, health?.snapshot_generated_at, onRefresh, reloadHealth]);

  return (
    <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
      <header className="flex min-h-[60px] shrink-0 flex-wrap items-center gap-x-4 gap-y-2 border-b border-line bg-bg px-4 py-3 sm:px-6">
        {backTo && (
          <button
            type="button"
            // History first, so this returns to the screen actually visited —
            // the anomaly feed, the filtered roster — rather than to whatever
            // section the page nominally belongs under.
            onClick={() => (canGoBack ? nav(-1) : nav(backTo))}
            className="shrink-0 cursor-pointer text-[12px] font-medium text-ink-3 transition-colors hover:text-ink"
          >
            ← Back
          </button>
        )}
        <div className="min-w-0 shrink-0">
          <h1 className="truncate text-[20px] font-bold tracking-tight text-ink-h">{title}</h1>
          {subtitle && <p className="mt-0.5 truncate text-[12px] text-ink-3">{subtitle}</p>}
        </div>

        {actions && (
          <div className="order-last flex w-full min-w-0 items-center gap-2 md:order-none md:w-auto md:flex-1">
            {actions}
          </div>
        )}
        {!actions && <div className="flex-1" />}

        <div className="flex shrink-0 items-center gap-3">
          {!noWindow && (
            <Select
              value={win}
              onChange={setWindow}
              options={Object.keys(WINDOWS)}
              label="Time window"
            />
          )}
          {/* Hidden below two tenants, so a single-customer deployment — or a
              failed `/api/tenants` — shows no picker rather than a misleading
              one. A tenant with no snapshot stays selectable and is badged:
              hiding it would hide the condition someone needs to act on. */}
          {tenants.length > 1 && (
            <Select
              value={tenant}
              onChange={setTenant}
              options={tenants.map((t) => t.id)}
              format={(id) => {
                const t = tenants.find((x) => x.id === id);
                if (!t) return id;
                return t.ready === false ? `${t.label} — no data` : t.label;
              }}
              label="Tenant"
            />
          )}
          {note && <span className="hidden text-[11px] text-ink-3 lg:inline">{note}</span>}
          {onRefresh && (
            <Btn
              variant="quiet"
              size="sm"
              onClick={refresh}
              disabled={busy}
              title="Re-read the current snapshot from S3"
            >
              <span className={busy ? "inline-flex motion-safe:animate-spin" : "inline-flex"}>
                <I.Refresh size={14} />
              </span>
            </Btn>
          )}
        </div>
      </header>

      <div className="flex-1 overflow-y-auto overflow-x-hidden">
        {/* Without the pipeline's cutoffs nothing on any screen can be
            coloured by severity or labelled with a band, and there is no
            fallback ladder to fall back on. Say so on every screen rather than
            letting panels render as though there were nothing to show. */}
        {tuningError && (
          <div className="px-4 pt-4 sm:px-6">
            <Notice>
              {SHOW_SCORING
                ? "Scoring parameters are unavailable, so severities and bands cannot be shown: "
                : "Detection parameters are unavailable, so the feed cannot be grouped: "}
              <span className="font-mono">{tuningError}</span>.{" "}
              <button
                type="button"
                onClick={reloadTuning}
                className="cursor-pointer font-semibold text-accent hover:underline"
              >
                Retry
              </button>
            </Notice>
          </div>
        )}
        {children}
      </div>
    </main>
  );
}

/** Standard content padding used by every section body. */
export function Pad({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`flex flex-col gap-4 p-4 sm:p-6 ${className}`}>{children}</div>;
}
