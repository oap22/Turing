import { useEffect, useMemo, useState } from "react";
import { applyFilter, toQuery } from "./filter";
import { EMPTY_FILTER, type TraceEvent, type TraceFilter } from "./types";

interface Props {
  liveEvents: TraceEvent[];
  /** Called when an event row is clicked — slice 7's wiring lights up the
   * matching call-graph edge briefly. */
  onSelect?: (event: TraceEvent) => void;
}

const HISTORY_PAGE = 200;

export default function TracePane({ liveEvents, onSelect }: Props) {
  const [history, setHistory] = useState<TraceEvent[]>([]);
  const [filter, setFilter] = useState<TraceFilter>(EMPTY_FILTER);
  const [expandedKey, setExpandedKey] = useState<string | null>(null);

  // Initial paint: fetch the last few minutes of history.
  useEffect(() => {
    const since = Date.now() - 5 * 60 * 1000;
    fetch(`/api/events?${toQuery(filter, since)}&limit=${HISTORY_PAGE}`, {
      credentials: "include",
    })
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
      className="flex h-full flex-col bg-neutral-950 font-mono text-xs"
    >
      <FilterBar filter={filter} onChange={setFilter} />
      <ol aria-label="Trace events" className="flex-1 overflow-auto">
        {merged.map((e) => {
          const key = `${e.node_name}/${e.event_type}/${e.timestamp_ms}/${e.seq ?? 0}`;
          const expanded = expandedKey === key;
          return (
            <li
              key={key}
              className="border-b border-neutral-800 hover:bg-neutral-900"
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
      <span className="text-neutral-500">{ts}</span>
      <span className="text-emerald-400">{event.node_name}</span>
      <span>{event.event_type}</span>
      <span className="text-right text-neutral-400">{dur}</span>
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
    <pre className="mt-1 max-h-64 overflow-auto rounded bg-neutral-900 p-2 text-[11px] text-neutral-400">
      {JSON.stringify(event.payload, null, 2)}
    </pre>
  );
}

function LlmExpansion({ event }: { event: TraceEvent }) {
  const p = event.payload as Record<string, unknown>;
  return (
    <div className="mt-1 space-y-1 rounded bg-neutral-900 p-2 text-neutral-300">
      <div>
        <span className="text-neutral-500">provider:</span> {String(p.provider ?? "?")} ·{" "}
        <span className="text-neutral-500">model:</span> {String(p.model ?? "?")}
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
    <div className="mt-1 rounded bg-neutral-900 p-2 text-neutral-300">
      <div>
        <span className="text-neutral-500">tool:</span> {String(p.tool ?? "?")}
      </div>
      <div>
        <span className="text-neutral-500">success:</span> {String(p.success ?? "?")}
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
      className="flex flex-wrap items-center gap-2 border-b border-neutral-800 bg-neutral-900 px-2 py-1 text-[11px] text-neutral-400"
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
        className="rounded border border-neutral-700 bg-neutral-950 px-2 py-1"
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
        className="rounded border border-neutral-700 bg-neutral-950 px-2 py-1"
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
        className="w-20 rounded border border-neutral-700 bg-neutral-950 px-2 py-1"
      />
    </div>
  );
}
