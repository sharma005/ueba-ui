import type { ReactNode } from "react";
import { sevClass, SHOW_SCORING } from "../theme";

export const cardSurface =
  "rounded-lg border border-line bg-card transition-shadow duration-200 hover:shadow-[var(--shadow-md)]";

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`${cardSurface} ${className}`}>{children}</div>;
}

export function CardHead({
  title,
  meta,
  className = "",
}: {
  title: ReactNode;
  meta?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`flex items-center justify-between gap-3 border-b border-line px-4 py-3 ${className}`}
    >
      <div className="caps text-ink">{title}</div>
      {meta && <div className="text-[11px] text-ink-3">{meta}</div>}
    </div>
  );
}

export interface Stat {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  color?: string;
  onClick?: () => void;
  /** Native tooltip — say where a clickable tile leads. */
  title?: string;
}

/* Column counts per tile count, so a StatBar never leaves a dead cell at the
   end of the row. `base` is the narrow-screen column count; the
   Tailwind class strings are spelled out because the JIT scanner cannot see
   an interpolated `grid-cols-${n}`. */
const statLayouts: Record<number, { base: number; cls: string }> = {
  1: { base: 1, cls: "grid-cols-1" },
  2: { base: 2, cls: "grid-cols-2" },
  3: { base: 1, cls: "grid-cols-1 lg:grid-cols-3" },
  4: { base: 2, cls: "grid-cols-2 lg:grid-cols-4" },
  5: { base: 2, cls: "grid-cols-2 lg:grid-cols-5" },
  6: { base: 2, cls: "grid-cols-2 lg:grid-cols-6" },
};

export function StatBar({ stats }: { stats: Stat[] }) {
  const layout = statLayouts[stats.length] ?? statLayouts[4];

  return (
    <div className={`grid overflow-hidden ${layout.cls} ${cardSurface}`}>
      {stats.map((s, i) => {
        const body = (
          <>
            <div className="caps-sm">{s.label}</div>
            <div
              className="mt-2 font-mono text-[26px] font-bold leading-none lg:text-[32px]"
              style={{ color: s.color ?? "var(--text-primary)" }}
            >
              {s.value}
            </div>
            {s.sub && <div className="mt-1.5 text-[11px] text-ink-3">{s.sub}</div>}
          </>
        );
        // Dividers are drawn per tile rather than with `first:`/`sm:` blanket
        // rules: which tile starts a row, and which sits on the last row,
        // changes with both the tile count and the breakpoint.
        const startsRow = i % layout.base === 0;
        const lastRow = i >= stats.length - (stats.length % layout.base || layout.base);
        const cell = [
          "border-line px-4 py-5 text-center transition-colors lg:px-6",
          startsRow ? "border-l-0" : "border-l",
          i === 0 ? "lg:border-l-0" : "lg:border-l",
          lastRow ? "" : "border-b",
          "lg:border-b-0",
        ].join(" ");

        return s.onClick ? (
          <button
            key={s.label}
            type="button"
            title={s.title}
            onClick={s.onClick}
            className={`${cell} w-full cursor-pointer hover:bg-card-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent`}
          >
            {body}
          </button>
        ) : (
          <div key={s.label} title={s.title} className={cell}>
            {body}
          </div>
        );
      })}
    </div>
  );
}

export function SevBadge({ sev, className = "" }: { sev: string; className?: string }) {
  // Single chokepoint for every severity badge in the console.
  if (!SHOW_SCORING) return null;
  return (
    <span
      className={`inline-block shrink-0 rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${sevClass(sev)} ${className}`}
    >
      {sev}
    </span>
  );
}

export function Badge({
  children,
  tone = "neutral",
  className = "",
  title,
}: {
  children: ReactNode;
  tone?: "neutral" | "accent" | "blue" | "critical" | "warn";
  className?: string;
  title?: string;
}) {
  const tones = {
    neutral: "text-ink-2 bg-hover border-line",
    accent: "text-accent bg-accent-bg border-accent-border",
    blue: "text-blue bg-[color-mix(in_srgb,var(--accent-blue)_10%,transparent)] border-[color-mix(in_srgb,var(--accent-blue)_25%,transparent)]",
    critical: "text-crit bg-[var(--sev-critical-bg)] border-[var(--sev-critical-border)]",
    warn: "text-med bg-[var(--sev-medium-bg)] border-[var(--sev-medium-border)]",
  }[tone];
  return (
    <span
      title={title}
      className={`inline-block rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${tones} ${className}`}
    >
      {children}
    </span>
  );
}

export function Btn({
  children,
  onClick,
  variant = "ghost",
  size = "md",
  className = "",
  disabled,
  title,
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "ghost" | "primary" | "danger" | "quiet";
  size?: "sm" | "md";
  className?: string;
  disabled?: boolean;
  title?: string;
}) {
  const variants = {
    ghost: "border-line bg-input text-ink-2 hover:text-ink hover:border-ink-3",
    quiet: "border-transparent bg-transparent text-ink-3 hover:text-ink hover:bg-hover",
    primary: "border-transparent bg-accent text-white hover:bg-accent-hover",
    danger: "border-[var(--sev-critical-border)] bg-[var(--sev-critical-bg)] text-crit hover:bg-crit hover:text-white",
  }[variant];
  const sizes = { sm: "px-2 py-1 text-[11px]", md: "px-3 py-1.5 text-[12px]" }[size];
  return (
    <button
      type="button"
      title={title}
      disabled={disabled}
      onClick={onClick}
      className={`inline-flex shrink-0 items-center justify-center gap-1.5 rounded-md border font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${variants} ${sizes} ${!disabled ? "cursor-pointer" : ""} ${className}`}
    >
      {children}
    </button>
  );
}

export function Bar({
  pct,
  color,
  height = 4,
  className = "",
}: {
  pct: number;
  color: string;
  height?: number;
  className?: string;
}) {
  return (
    <div
      className={`overflow-hidden rounded-full bg-hover ${className}`}
      style={{ height }}
      role="presentation"
    >
      <div
        className="rounded-full transition-[width]"
        style={{ height, width: `${Math.max(0, Math.min(100, pct))}%`, background: color }}
      />
    </div>
  );
}

export function Toggle({ on, onChange }: { on: boolean; onChange: () => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      onClick={onChange}
      className="relative h-5 w-9 shrink-0 cursor-pointer rounded-full transition-colors"
      style={{ background: on ? "var(--accent)" : "var(--border-primary)" }}
    >
      <span
        className="absolute top-0.5 block h-4 w-4 rounded-full bg-white shadow-sm transition-[left]"
        style={{ left: on ? 18 : 2 }}
      />
    </button>
  );
}

export function Select({
  value,
  onChange,
  options,
  format,
  label,
}: {
  value: string;
  onChange: (v: string) => void;
  options: string[];
  format?: (v: string) => string;
  label: string;
}) {
  return (
    <select
      value={value}
      aria-label={label}
      onChange={(e) => onChange(e.target.value)}
      className="cursor-pointer rounded-md border border-line bg-input px-2.5 py-1.5 text-[12px] font-medium text-ink-2 outline-none transition-colors hover:text-ink focus:border-accent"
    >
      {options.map((o) => (
        <option key={o} value={o}>
          {format ? format(o) : o}
        </option>
      ))}
    </select>
  );
}

export function Avatar({
  initials,
  color,
  size = 36,
}: {
  initials: string;
  color: string;
  size?: number;
}) {
  return (
    <span
      className="flex shrink-0 items-center justify-center rounded-full font-bold text-white"
      style={{ width: size, height: size, background: color, fontSize: size * 0.36 }}
    >
      {initials}
    </span>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-1.5 px-6 py-14 text-center">
      <div className="text-[13px] font-semibold text-ink-2">{title}</div>
      {hint && <div className="max-w-md text-[12px] leading-relaxed text-ink-3">{hint}</div>}
    </div>
  );
}

/** Amber strip explaining that a capability has no producer in the pipeline. */
export function Notice({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-md border border-[var(--sev-medium-border)] bg-[var(--sev-medium-bg)] px-3.5 py-2.5 text-[12px] leading-relaxed text-med">
      {children}
    </div>
  );
}
