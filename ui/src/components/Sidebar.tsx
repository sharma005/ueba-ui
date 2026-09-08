import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import type { Mode } from "../theme";
import * as I from "./icons";
import { cardSurface } from "./primitives";

export interface NavItem {
  to: string;
  label: string;
  icon: ReactNode;
  badgeKey?: keyof NavCounts;
}

/** Null while the count is in flight or its request failed — no badge shown. */
export interface NavCounts {
  anomalies?: number | null;
  alerts?: number | null;
}

export const NAV: NavItem[] = [
  { to: "/", label: "Dashboard", icon: <I.Dashboard /> },
  { to: "/users", label: "Users", icon: <I.Users /> },
  { to: "/anomalies", label: "Anomalies", icon: <I.Warning />, badgeKey: "anomalies" },
  { to: "/alerts", label: "Alerts", icon: <I.Bell />, badgeKey: "alerts" },
];

const THEMES: { value: Mode; label: string; Icon: typeof I.Sun }[] = [
  { value: "light", label: "Light", Icon: I.Sun },
  { value: "dark", label: "Dark", Icon: I.Moon },
];

function ThemeToggle({ mode, onSetMode }: { mode: Mode; onSetMode: (m: Mode) => void }) {
  return (
    <div
      role="group"
      aria-label="Theme"
      className="flex w-fit items-center rounded-[10px] border border-line bg-input p-[3px]"
    >
      {THEMES.map(({ value, label, Icon }) => {
        const on = mode === value;
        return (
          <button
            key={value}
            type="button"
            onClick={() => onSetMode(value)}
            aria-pressed={on}
            className={`flex cursor-pointer items-center gap-[5px] rounded-[7px] px-2.5 py-[5px] text-[12px] font-medium transition-colors duration-[120ms] ${
              on ? "bg-accent-bg text-accent" : "text-ink-3 hover:text-ink-2"
            }`}
          >
            <Icon size={13} />
            {label}
          </button>
        );
      })}
    </div>
  );
}

export function Sidebar({
  counts,
  collapsed,
  onToggleCollapse,
  mode,
  onSetMode,
  ingest,
}: {
  counts: NavCounts;
  collapsed: boolean;
  onToggleCollapse: () => void;
  mode: Mode;
  onSetMode: (m: Mode) => void;
  ingest: { label: string; detail: string; ok: boolean };
}) {
  return (
    <nav
      className="flex shrink-0 flex-col border-r border-line bg-sidebar transition-[width] duration-200"
      style={{ width: collapsed ? 68 : 220 }}
      aria-label="Sections"
    >
      <div className={`flex h-[60px] items-center gap-2.5 border-b border-line ${collapsed ? "justify-center px-0" : "px-4"}`}>
        <I.LogoMark className="h-[20px] w-[20px] shrink-0" />
        {!collapsed && (
          <div className="min-w-0 flex-1">
            <div className="text-[15px] font-extrabold leading-tight tracking-tight text-ink">UEBA</div>
            <div className="text-[8.5px] font-semibold uppercase tracking-[0.12em] text-ink-3">
              Behavior Analytics
            </div>
          </div>
        )}
      </div>


      {!collapsed && (
        <div className="px-3 pt-3">
          <ThemeToggle mode={mode} onSetMode={onSetMode} />
        </div>
      )}

      <ul className="flex flex-col gap-0.5 py-2">
        {NAV.map((n) => {
          const badge = n.badgeKey ? counts[n.badgeKey] : undefined;
          return (
            <li key={n.to}>
              <NavLink
                to={n.to}
                end={n.to === "/"}
                title={collapsed ? n.label : undefined}
                className={({ isActive }) =>
                  `relative mx-2 flex items-center gap-3 rounded-md px-3 py-2.5 text-[13px] font-medium transition-colors ${
                    isActive
                      ? "bg-accent-bg font-semibold text-accent"
                      : "text-ink-2 hover:bg-hover hover:text-ink"
                  } ${collapsed ? "justify-center px-0" : ""}`
                }
              >
                {({ isActive }) => (
                  <>
                    {isActive && (
                      <span className="absolute -left-2 top-1.5 bottom-1.5 w-[3px] rounded-r bg-accent" />
                    )}
                    <span className="shrink-0">{n.icon}</span>
                    {!collapsed && <span className="flex-1 truncate">{n.label}</span>}
                    {badge != null && badge > 0 && (
                      <span
                        title={`${badge}`}
                        className={
                          collapsed
                            ? "absolute right-1.5 top-0.5 min-w-[16px] rounded-full bg-crit px-1 py-px text-center text-[9px] font-bold leading-tight text-white"
                            : "shrink-0 rounded-full bg-crit px-1.5 py-0.5 text-[10px] font-bold leading-none text-white"
                        }
                      >
                        {collapsed ? (badge > 99 ? "99+" : badge) : badge > 999 ? "999+" : badge}
                      </span>
                    )}
                  </>
                )}
              </NavLink>
            </li>
          );
        })}
      </ul>

      <div className="flex-1" />

      {!collapsed && (
        <div className={`mx-3 mb-2 px-3 py-2.5 ${cardSurface}`}>
          <div className="flex items-center gap-2">
            <span
              className={`h-1.5 w-1.5 shrink-0 rounded-full ${ingest.ok ? "bg-accent pulse" : "bg-high"}`}
            />
            <span className="truncate text-[10.5px] font-semibold uppercase tracking-wider text-ink-2">
              {ingest.label}
            </span>
          </div>
          <div className="mt-1 text-[10.5px] leading-snug text-ink-3">{ingest.detail}</div>
        </div>
      )}

      <div className="border-t border-line px-3 py-2">
        <button
          type="button"
          onClick={onToggleCollapse}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          className="flex w-full cursor-pointer items-center justify-center rounded-md py-1.5 text-ink-3 transition-colors hover:bg-hover hover:text-ink"
        >
          <I.Collapse size={16} flipped={collapsed} />
        </button>
      </div>
    </nav>
  );
}
