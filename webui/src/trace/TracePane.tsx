import { useEffect, useMemo, useState } from "react";
import { gatewayFetch } from "../desktop/gateway";
import { applyFilter, toQuery } from "./filter";
import { EMPTY_FILTER, type TraceEvent, type TraceFilter } from "./types";

interface Props {
  liveEvents: TraceEvent[];
  /** Called when an event row is clicked — slice 7's wiring lights up the
   * matching call-graph edge briefly. */
  onSelect?: (event: TraceEvent) => void;
}

const HISTORY_PAGE = 200;

// Family colour for an event type — mirrors the TUI's trace palette so the
// stream stays scannable without becoming a rainbow.
function eventClass(eventType: string): string {
  if (eventType.endsWith(".error")) return "text-rose-400";
  const family = eventType.split(".")[0];
  if (family === "llm") return "text-fuchsia-300";
  if (family === "tool") return "text-cyan-300";
  if (family === "mesh" || family === "presence") return "text-emerald-300";
  return "text-term-fg";
}

const FILTER_INPUT =
  "border border-term-edge bg-term-bg px-2 py-1 text-term-fg placeholder:text-term-dim focus:border-term-accent focus:outline-none";

export default function TracePane({ liveEvents, onSelect }: Props) {
  const [history, setHistory] = useState<TraceEvent[]>([]);
  const [filter, setFilter] = useState<TraceFilter>(EMPTY_FILTER);
  const [expandedKey, setExpandedKey] = useState<string | null>(null);

  // Initial paint: fetch the last few minutes of history.
  useEffect(() => {
    const since = Date.now() - 5 * 60 * 1000;
    gatewayFetch(`/api/events?${toQuery(filter, since)}&limit=${HISTORY_PAGE}`)
      .then((r) => (r.ok ? r.json() : { events: [] }))
      .then((body: { events: TraceEvent[] }) => setHistory(body.events ?? []))
      .catch(() => setHistory([]));
  }, [filter]);

  const merged = useMemo(() => {
    // Live events arrive in real time; combine with history and dedupe by
    // (node, event_type, seq) so a row that appears in both doesn't double up.
    const seen = new Set<string>();
    const out: TraceEvent[] = [];
    for (const e of [...history, ...liveEvents]) {
      const key = `${e.node_name}::${e.event_type}::${e.seq ?? ""}::${e.timestamp_ms}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(e);
    }
    out.sort((a, b) => b.timestamp_ms - a.timestamp_ms);
    return applyFilter(out, filter);
  }, [history, liveEvents, filter]);

  return (
    <div
      aria-label="Message trace"
      className="flex h-full flex-col bg-term-bg font-mono text-xs"
    >
      <div className="border-b border-term-edge bg-term-panel px-3 py-1.5">
        <span className="text-[11px] font-bold uppercase tracking-widest text-term-fg">
          trace
        </span>
        <span className="ml-2 text-[10px] uppercase tracking-wider text-term-dim">
          {merged.length} event{merged.length === 1 ? "" : "s"}
        </span>
      </div>
      <FilterBar filter={filter} onChange={setFilter} />
      <ol aria-label="Trace events" className="flex-1 overflow-auto">
        {merged.map((e) => {
          const key = `${e.node_name}/${e.event_type}/${e.timestamp_ms}/${e.seq ?? 0}`;
          const expanded = expandedKey === key;
          return (
            <li
              key={key}
              className="border-b border-term-edge/60 hover:bg-term-raised"
            >
              <button
                type="button"
                aria-expanded={expanded}
                aria-label={`${expanded ? "Collapse" : "Expand"} trace event ${
                  e.event_type
                } from ${e.node_name}`}
                className="w-full px-2 py-1 text-left"
                onClick={() => {
                  onSelect?.(e);
                  setExpandedKey((prev) => (prev === key ? null : key));
                }}
              >
                <Row event={e} />
              </button>
              {expanded && (
                <div className="px-2 pb-1">
                  <Expansion event={e} />
                </div>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function Row({ event }: { event: TraceEvent }) {
  const ts = new Date(event.timestamp_ms).toISOString().slice(11, 23);
  const dur = event.duration_ms !== null ? `${Math.round(event.duration_ms)}ms` : "";
  return (
    <div className="grid grid-cols-[80px_120px_1fr_70px] gap-2 text-neutral-300">
      <span className="text-term-dim">{ts}</span>
      <span className="text-term-accent">{event.node_name}</span>
      <span className={eventClass(event.event_type)}>{event.event_type}</span>
      <span className="text-right tabular-nums text-term-dim">{dur}</span>
    </div>
  );
}

function Expansion({ event }: { event: TraceEvent }) {
  if (event.event_type.startsWith("llm.")) {
    return <LlmExpansion event={event} />;
  }
  if (event.event_type.startsWith("tool.")) {
    return <ToolExpansion event={event} />;
  }
  return (
    <pre className="mt-1 max-h-64 overflow-auto border border-term-edge bg-term-panel p-2 text-[11px] text-neutral-400">
      {JSON.stringify(event.payload, null, 2)}
    </pre>
  );
}

function LlmExpansion({ event }: { event: TraceEvent }) {
  const p = event.payload as Record<string, unknown>;
  return (
    <div className="mt-1 space-y-1 border border-term-edge bg-term-panel p-2 text-neutral-300">
      <div>
        <span className="text-term-dim">provider:</span> {String(p.provider ?? "?")} ·{" "}
        <span className="text-term-dim">model:</span> {String(p.model ?? "?")}
      </div>
      {p.prompt_sample !== undefined && (
        <details>
          <summary className="cursor-pointer text-neutral-400">prompt (redacted)</summary>
          <pre className="mt-1 whitespace-pre-wrap break-words text-[11px]">
            {String(p.prompt_sample)}
          </pre>
        </details>
      )}
      {p.response_sample !== undefined && (
        <details>
          <summary className="cursor-pointer text-neutral-400">response (redacted)</summary>
          <pre className="mt-1 whitespace-pre-wrap break-words text-[11px]">
            {String(p.response_sample)}
          </pre>
        </details>
      )}
    </div>
  );
}

function ToolExpansion({ event }: { event: TraceEvent }) {
  const p = event.payload as Record<string, unknown>;
  return (
    <div className="mt-1 border border-term-edge bg-term-panel p-2 text-neutral-300">
      <div>
        <span className="text-term-dim">tool:</span> {String(p.tool ?? "?")}
      </div>
      <div>
        <span className="text-term-dim">success:</span> {String(p.success ?? "?")}
      </div>
      {event.error && (
        <div className="text-rose-400">error: {String(event.error)}</div>
      )}
    </div>
  );
}

function FilterBar({
  filter,
  onChange,
}: {
  filter: TraceFilter;
  onChange: (next: TraceFilter) => void;
}) {
  return (
    <div
      role="search"
      aria-label="Trace filters"
      className="flex flex-wrap items-center gap-2 border-b border-term-edge bg-term-panel px-2 py-1 text-[11px] text-term-dim"
    >
      <label className="sr-only" htmlFor="trace-filter-nodes">
        Filter trace by nodes
      </label>
      <input
        id="trace-filter-nodes"
        type="text"
        placeholder="nodes (csv)"
        value={filter.nodes.join(",")}
        onChange={(e) =>
          onChange({
            ...filter,
            nodes: e.target.value.split(",").map((s) => s.trim()).filter(Boolean),
          })
        }
        className={FILTER_INPUT}
      />
      <label className="sr-only" htmlFor="trace-filter-events">
        Filter trace by event types
      </label>
      <input
        id="trace-filter-events"
        type="text"
        placeholder="event types (csv)"
        value={filter.eventTypes.join(",")}
        onChange={(e) =>
          onChange({
            ...filter,
            eventTypes: e.target.value.split(",").map((s) => s.trim()).filter(Boolean),
          })
        }
        className={FILTER_INPUT}
      />
      <label className="sr-only" htmlFor="trace-filter-duration">
        Minimum duration in milliseconds
      </label>
      <input
        id="trace-filter-duration"
        type="number"
        placeholder="min ms"
        value={filter.minDurationMs ?? ""}
        onChange={(e) =>
          onChange({
            ...filter,
            minDurationMs: e.target.value === "" ? null : Number(e.target.value),
          })
        }
        className={`w-20 ${FILTER_INPUT}`}
      />
    </div>
  );
}
