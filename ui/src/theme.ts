
export type Mode = "light" | "dark";
export type Sev = "Critical" | "High" | "Medium" | "Low";
export type Band = "notable" | "high" | "medium" | "low";

/** Score cutoffs from `/api/tuning` — `derive.BANDS` in the pipeline. */
export interface ScoreBands {
  notable: number;
  high: number;
  medium: number;
}

/** Weight cutoffs from `/api/tuning` — `catalog.SEVERITY_BANDS` lows. */
export interface SeverityBands {
  critical: number;
  error: number;
  warning: number;
}

const STORAGE_KEY = "cx-ueba-theme";

export interface Palette {
  bg: string;
  surface: string;
  card: string;
  cardHover: string;
  sidebar: string;
  input: string;
  ink: string;
  ink2: string;
  ink3: string;
  /** Non-text grey: strokes, arrowheads, low-signal graph nodes. */
  dim: string;
  accent: string;
  blue: string;
  purple: string;
  pink: string;
  crit: string;
  high: string;
  med: string;
  low: string;
  line: string;
}

const DARK: Palette = {
  bg: "#202020",
  surface: "#1a1a1a",
  card: "#262626",
  cardHover: "#2e2e2e",
  sidebar: "#1a1a1a",
  input: "#2a2a2a",
  ink: "#e0ddd5",
  ink2: "#9a9589",
  ink3: "#6b675c",
  dim: "#6e6e6e",
  accent: "#00C853",
  blue: "#3b82f6",
  purple: "#8b5cf6",
  pink: "#ec4899",
  crit: "#ef4444",
  high: "#f97316",
  med: "#f59e0b",
  low: "#00C853",
  line: "#333333",
};

const LIGHT: Palette = {
  ...DARK,
  bg: "#f5f5f7",
  surface: "#ffffff",
  card: "#ffffff",
  cardHover: "#f9fafb",
  sidebar: "#ffffff",
  input: "#f3f4f6",
  ink: "#111827",
  ink2: "#4b5563",
  ink3: "#9ca3af",
  dim: "#9ca3af",
  accent: "#00a844",
  blue: "#2563eb",
  purple: "#7c3aed",
  pink: "#db2777",
  crit: "#dc2626",
  high: "#ea580c",
  med: "#d97706",
  low: "#00a844",
  line: "#e5e7eb",
};

/** Palette for the mode currently on `<html data-theme>`. */
export function themeColors(mode?: Mode): Palette {
  const m = mode ?? currentMode();
  return m === "light" ? LIGHT : DARK;
}

export function currentMode(): Mode {
  if (typeof document === "undefined") return "dark";
  return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
}

export function applyMode(mode: Mode) {
  document.documentElement.setAttribute("data-theme", mode);
  try {
    localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    // Private browsing or blocked site data — the theme just won't persist.
  }
}

export function initialMode(): Mode {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    // ignore
  }
  if (typeof matchMedia === "undefined") return "dark";
  return matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

/**
 * Master switch for every risk-scoring affordance in the console: entity
 * scores, band labels, anomaly severity, and the colours that encode them.
 *
 * Turned off, the UI is purely descriptive — who, what detection, when, how
 * many — while the pipeline keeps scoring and the API keeps serving `score`,
 * `band` and `weight`, and rows stay ordered by score underneath. Nothing but
 * this constant decides which way the console reads.
 *
 * Gated centrally in `sevColor`/`sevClass` below and in `SevBadge`, so the
 * ~40 call sites that only ask for a colour or a badge need no change.
 */
export const SHOW_SCORING = true;

export const sevColor = (s: string, p: Palette = themeColors()): string =>
  !SHOW_SCORING ? p.dim
    : s === "Critical" ? p.crit : s === "High" ? p.high : s === "Medium" ? p.med : s === "Low" ? p.low : p.ink3;

/** Tailwind-friendly class pair for a severity badge. */
export function sevClass(s: string): string {
  if (!SHOW_SCORING) return "text-ink-2 bg-hover border-line";
  switch (s) {
    case "Critical":
      return "text-[var(--sev-critical)] bg-[var(--sev-critical-bg)] border-[var(--sev-critical-border)]";
    case "High":
      return "text-[var(--sev-high)] bg-[var(--sev-high-bg)] border-[var(--sev-high-border)]";
    case "Medium":
      return "text-[var(--sev-medium)] bg-[var(--sev-medium-bg)] border-[var(--sev-medium-border)]";
    default:
      return "text-[var(--sev-low)] bg-[var(--sev-low-bg)] border-[var(--sev-low-border)]";
  }
}

/**
 * Score -> band, using the cutoffs `/api/tuning` served.
 *
 * There is deliberately no default: these numbers belong to `derive.BANDS` in
 * the pipeline, and a fallback copy here is exactly how the UI came to paint a
 * critical threshold of 85 against a pipeline scoring at 90. Callers that have
 * no bands yet must render "—", not a guess.
 *
 * Only for bare scores like `kpis.max_score`. Anything carrying the server's
 * own `band` field should use `bandToSev` on that instead of re-deriving it.
 */
export function levelOf(score: number, bands: ScoreBands): { label: string; sev: Sev } {
  if (score >= bands.notable) return { label: "CRITICAL", sev: "Critical" };
  if (score >= bands.high) return { label: "HIGH", sev: "High" };
  if (score >= bands.medium) return { label: "MEDIUM", sev: "Medium" };
  return { label: "LOW", sev: "Low" };
}

export const bandToSev = (b: Band): Sev =>
  b === "notable" ? "Critical" : b === "high" ? "High" : b === "medium" ? "Medium" : "Low";

/**
 * Anomaly weight -> severity, using the ladder `/api/tuning` served from
 * `catalog.SEVERITY_BANDS`. Same rule as `levelOf`: no default ladder, because
 * three different ones had grown across the UI and one finding could read
 * Critical on the feed and High on the timeline.
 */
export const weightToSev = (w: number, bands: SeverityBands): Sev =>
  w >= bands.critical ? "Critical"
    : w >= bands.error ? "High"
      : w >= bands.warning ? "Medium"
        : "Low";

/** Categorical series colours for the donut / multi-series charts. */
export function categorical(p: Palette = themeColors()): string[] {
  return [p.accent, p.blue, p.purple, p.pink, p.med, p.high, p.crit, "#14b8a6"];
}
