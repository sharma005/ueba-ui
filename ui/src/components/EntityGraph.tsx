import { useEffect, useRef, useState } from "react";
import ForceGraph2D from "react-force-graph-2d";
import { SHOW_SCORING, themeColors, type Palette, type ScoreBands } from "../theme";
import { useMode } from "./Shell";
import type { GraphData } from "../types";

/**
 * Node colour by node type. `asn` and `country` deliberately have no entry and
 * fall through to the neutral tone — see `routes.entity_graph`, which says so.
 */
function typeColors(p: Palette): Record<string, string> {
  return { user: p.blue, ip: p.dim, app: p.accent };
}

export function EntityGraph({
  data,
  height = 420,
  onNodeClick,
  bands,
}: {
  data: GraphData;
  height?: number;
  onNodeClick?: (id: string, type: string) => void;
  /** Score cutoffs from `/api/tuning`. Required: there is no default ladder. */
  bands: ScoreBands;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(600);
  const mode = useMode();
  const p = themeColors(mode);
  const byType = typeColors(p);
  // The types actually present, so the legend describes this graph rather than
  // a fixed list including kinds this pipeline never emits (host, domain).
  const presentTypes = [...new Set(data.nodes.map((n) => n.type))];

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    setWidth(el.clientWidth);
    return () => ro.disconnect();
  }, []);

  const graph = {
    nodes: data.nodes.map((n) => ({ ...n })),
    links: data.links.map((l) => ({ ...l })),
  };

  return (
    <div ref={wrapRef} className="overflow-hidden">
      <ForceGraph2D
        graphData={graph}
        width={width}
        height={height}
        backgroundColor={p.card}
        linkColor={() => p.line}
        // `Math.max(1, …)`: an edge weight of 0 is legal (the profile counter
        // can be zero) and log2(0) is -Infinity, which is not a valid width.
        linkWidth={(l: any) => Math.min(1 + Math.log2(Math.max(1, l.weight ?? 1)), 4)}
        nodeCanvasObject={(node: any, ctx, scale) => {
          // Node size and colour encode the score bands, so both collapse to
          // the neutral by-type styling when scoring is hidden.
          const critical = SHOW_SCORING && node.score >= bands.notable;
          const risky = SHOW_SCORING && node.score >= bands.high;
          const r = node.id === data.entity ? 9 : risky ? 7 : 5;
          ctx.beginPath();
          ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
          ctx.fillStyle = critical ? p.crit : risky ? p.high : (byType[node.type] ?? p.dim);
          ctx.fill();
          if (node.id === data.entity || risky) {
            ctx.strokeStyle = risky ? p.crit : p.accent;
            ctx.lineWidth = 1.5 / scale;
            ctx.stroke();
          }
          if (scale > 1.2 || node.id === data.entity || risky) {
            ctx.font = `${11 / scale}px Inter, sans-serif`;
            ctx.fillStyle = p.ink2;
            ctx.textAlign = "center";
            ctx.fillText(String(node.id).slice(0, 24), node.x, node.y + r + 10 / scale);
          }
        }}
        onNodeClick={(n: any) => onNodeClick?.(n.id, n.type)}
        cooldownTicks={80}
      />
      <div className="flex flex-wrap gap-4 border-t border-line px-4 py-2.5 text-[11.5px] text-ink-2">
        {presentTypes.map((t) => (
          <span key={t} className="inline-flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-full" style={{ background: byType[t] ?? p.dim }} /> {t}
          </span>
        ))}
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full" style={{ background: p.high }} /> risk ≥ {bands.high}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full" style={{ background: p.crit }} /> risk ≥ {bands.notable}
        </span>
      </div>
    </div>
  );
}
