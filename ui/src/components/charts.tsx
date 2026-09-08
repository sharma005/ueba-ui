import { useId } from "react";
import { themeColors } from "../theme";
import { useMode } from "./Shell";

export interface Slice {
  label: string;
  value: number;
  color: string;
}

export function Donut({
  slices,
  size = 220,
  thickness = 46,
  onSliceClick,
}: {
  slices: Slice[];
  size?: number;
  thickness?: number;
  onSliceClick?: (s: Slice) => void;
}) {
  const total = slices.reduce((n, s) => n + s.value, 0);
  const r = (size - thickness) / 2;
  const circumference = 2 * Math.PI * r;

  let offset = 0;
  const arcs = slices.map((s) => {
    const frac = total > 0 ? s.value / total : 0;
    const arc = { ...s, frac, dash: frac * circumference, offset };
    offset += frac * circumference;
    return arc;
  });

  return (
    <div className="flex w-full min-w-0 flex-wrap items-center justify-center gap-6">
      <svg
        viewBox={`0 0 ${size} ${size}`}
        className="h-auto w-[160px] shrink-0 sm:w-[200px]"
        role="img"
        aria-label="Anomaly types"
      >
        <g transform={`rotate(-90 ${size / 2} ${size / 2})`}>
          {arcs.map((a) => (
            <circle
              key={a.label}
              cx={size / 2}
              cy={size / 2}
              r={r}
              fill="none"
              stroke={a.color}
              strokeWidth={thickness}
              strokeDasharray={`${a.dash} ${circumference - a.dash}`}
              strokeDashoffset={-a.offset}
              onClick={onSliceClick ? () => onSliceClick(a) : undefined}
              style={{ cursor: onSliceClick ? "pointer" : undefined }}
            >
              <title>{`${a.label} — ${a.value} (${Math.round(a.frac * 100)}%)`}</title>
            </circle>
          ))}
        </g>
      </svg>

      {/* `basis` + `min-w-0` rather than a min-width: a long type name used to
          hold the legend wider than the card and push itself off the right
          edge, clipped mid-word. Now it wraps under the ring when the card is
          narrow, and any single label that is still too long truncates. */}
      <ul className="flex min-w-0 flex-1 basis-[180px] flex-col gap-2.5">
        {slices.map((s) => (
          <li key={s.label} className="flex min-w-0 items-start gap-2.5 text-[11px] text-ink-2">
            <span className="mt-[3px] h-2 w-2 shrink-0 rounded-full" style={{ background: s.color }} />
            <span className="min-w-0 flex-1 truncate leading-snug" title={s.label}>
              {s.label}
            </span>
            <span className="shrink-0 font-mono text-[10px] text-ink-3">{s.value}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export interface Series {
  name: string;
  color: string;
  values: number[];
  fill?: boolean;
  width?: number;
}

export function LineChart({
  series,
  labels,
  height = 200,
  max: maxOverride,
  threshold,
  yTicks = 5,
}: {
  series: Series[];
  labels?: string[];
  height?: number;
  max?: number;
  threshold?: number;
  yTicks?: number;
}) {
  const uid = useId().replace(/:/g, "");
  const mode = useMode();
  const c = themeColors(mode);

  const W = 800;
  const H = 220;
  const L = 34;
  const R = 8;
  const T = 10;
  const B = 190;

  const n = Math.max(...series.map((s) => s.values.length), 2);
  const dataMax = Math.max(1, maxOverride ?? Math.max(...series.flatMap((s) => s.values), threshold ?? 0));

  const x = (i: number) => L + (n === 1 ? 0 : (i / (n - 1)) * (W - L - R));
  const y = (v: number) => B - (Math.max(0, v) / dataMax) * (B - T);
  const pts = (vals: number[]) => vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");

  const ticks = Array.from({ length: yTicks + 1 }, (_, i) => Math.round((dataMax / yTicks) * i));

  return (
    <>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        style={{ width: "100%", height, display: "block" }}
        role="img"
        aria-label={series.map((s) => s.name).join(", ")}
      >
        <defs>
          {series
            .filter((s) => s.fill)
            .map((s) => (
              <linearGradient key={s.name} id={`g-${uid}-${slug(s.name)}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={s.color} stopOpacity="0.25" />
                <stop offset="100%" stopColor={s.color} stopOpacity="0" />
              </linearGradient>
            ))}
        </defs>

        {ticks.map((t) => (
          <g key={t}>
            <line x1={L} y1={y(t)} x2={W - R} y2={y(t)} stroke={c.line} strokeWidth="1" opacity="0.5" />
            <text
              x={L - 6}
              y={y(t) + 3.5}
              textAnchor="end"
              fill={c.ink2}
              fontSize="9"
              fontFamily="JetBrains Mono, monospace"
            >
              {t}
            </text>
          </g>
        ))}

        {threshold !== undefined && (
          <line
            x1={L}
            y1={y(threshold)}
            x2={W - R}
            y2={y(threshold)}
            stroke={c.crit}
            strokeWidth="1"
            strokeDasharray="4 4"
            opacity="0.7"
          />
        )}

        {series
          .filter((s) => s.fill && s.values.length > 1)
          .map((s) => (
            <polygon
              key={`a-${s.name}`}
              points={`${pts(s.values)} ${W - R},${B} ${L},${B}`}
              fill={`url(#g-${uid}-${slug(s.name)})`}
            />
          ))}

        {series
          .filter((s) => s.values.length > 1)
          .map((s) => (
            <polyline
              key={`l-${s.name}`}
              points={pts(s.values)}
              fill="none"
              stroke={s.color}
              strokeWidth={s.width ?? 2}
              strokeLinejoin="round"
              strokeLinecap="round"
            />
          ))}
      </svg>

      {labels && labels.length > 0 && (
        <div
          className="flex justify-between font-mono text-[10px] text-ink-2"
          style={{ paddingLeft: `${(L / W) * 100}%` }}
        >
          {labels.map((l, i) => (
            <span key={`${l}-${i}`}>{l}</span>
          ))}
        </div>
      )}
    </>
  );
}

export interface RadarAxis {
  label: string;
  /** 0–1 share of the axis maximum. */
  value: number;
  hint?: string;
}

export function Radar({ axes, size = 240 }: { axes: RadarAxis[]; size?: number }) {
  const mode = useMode();
  const c = themeColors(mode);

  const cx = size / 2;
  const cy = size / 2;
  const r = size / 2 - 34;
  const n = Math.max(3, axes.length);

  const point = (i: number, frac: number) => {
    const a = (i / n) * Math.PI * 2 - Math.PI / 2;
    return [cx + Math.cos(a) * r * frac, cy + Math.sin(a) * r * frac] as const;
  };

  const rings = [0.25, 0.5, 0.75, 1];
  const shape = axes.map((ax, i) => point(i, Math.max(0.04, Math.min(1, ax.value))).join(",")).join(" ");

  return (
    <div className="px-16 py-4">
      <svg
        width={size}
        height={size}
        viewBox={`0 0 ${size} ${size}`}
        overflow="visible"
        role="img"
        aria-label="Behavior profile"
      >
      {rings.map((ring) => (
        <polygon
          key={ring}
          points={axes.map((_, i) => point(i, ring).join(",")).join(" ")}
          fill="none"
          stroke={c.line}
          strokeWidth="1"
          opacity={ring === 1 ? 0.9 : 0.45}
        />
      ))}

      {axes.map((_, i) => {
        const [px, py] = point(i, 1);
        return <line key={i} x1={cx} y1={cy} x2={px} y2={py} stroke={c.line} strokeWidth="1" opacity="0.45" />;
      })}

      <polygon points={shape} fill={c.accent} fillOpacity="0.18" stroke={c.accent} strokeWidth="2" />

      {axes.map((ax, i) => {
        const [px, py] = point(i, Math.max(0.04, Math.min(1, ax.value)));
        return (
          <circle key={ax.label} cx={px} cy={py} r="3.5" fill={c.accent}>
            <title>{ax.hint ?? `${ax.label}: ${Math.round(ax.value * 100)}%`}</title>
          </circle>
        );
      })}

      {axes.map((ax, i) => {
        const [lx, ly] = point(i, 1.2);
        const anchor = Math.abs(lx - cx) < 6 ? "middle" : lx > cx ? "start" : "end";
        return (
          <text
            key={`t-${ax.label}`}
            x={lx}
            y={ly + 3}
            textAnchor={anchor}
            fill={c.ink2}
            fontSize="9.5"
          >
            {ax.label}
          </text>
        );
      })}
      </svg>
    </div>
  );
}

const slug = (s: string) => s.replace(/[^a-z0-9]/gi, "");
