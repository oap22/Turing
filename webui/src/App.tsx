import { useEffect, useMemo, useReducer, useState } from "react";
import AlertBanner from "./alerts/AlertBanner";
import {
  applyAlert,
  emptyAlerts,
  listAlerts,
  type AlertsState,
} from "./alerts/reducer";
import type { AlertFrame } from "./alerts/types";
import CallGraphCanvas from "./CallGraphCanvas";
import ChatPane from "./chat/ChatPane";
import {
  applyChatFrame,
  emptyChat,
  listChat,
  type ChatState,
} from "./chat/reducer";
import { emptyState, markStale, reduce, type Frame } from "./graph/reducer";
import QueuePane from "./queue/QueuePane";
import {
  applyQueueFrame,
  emptyQueue,
  listQueue,
  type QueueState,
} from "./queue/reducer";
import SpecsGrid from "./specs/SpecsGrid";
import type { PeerSpecsRow } from "./specs/types";
import TracePane from "./trace/TracePane";
import type { TraceEvent } from "./trace/types";
import { connectGatewayWS, type ChatFrame, type QueueFrame } from "./ws";

type AlertAction = { kind: "frame"; frame: AlertFrame };

function alertsReducer(state: AlertsState, action: AlertAction): AlertsState {
  if (action.kind === "frame") return applyAlert(state, action.frame);
  return state;
}

type QueueReducerAction = { kind: "frame"; frame: QueueFrame };

function queueReducer(state: QueueState, action: QueueReducerAction): QueueState {
  if (action.kind === "frame") return applyQueueFrame(state, action.frame);
  return state;
}

type ChatReducerAction = { kind: "frame"; frame: ChatFrame };

function chatReducer(state: ChatState, action: ChatReducerAction): ChatState {
  if (action.kind === "frame") return applyChatFrame(state, action.frame);
  return state;
}

const STALE_TICK_MS = 5_000;
const HIGHLIGHT_MS = 1_500;
const PEERS_POLL_MS = 10_000;

export default function App() {
  const [state, dispatch] = useReducer(
    (s: ReturnType<typeof emptyState>, f: Frame) => reduce(s, f),
    emptyState(),
  );
  const [alertsState, alertsDispatch] = useReducer(alertsReducer, emptyAlerts());
  const alerts = useMemo(() => listAlerts(alertsState), [alertsState]);
  const [queueState, queueDispatch] = useReducer(queueReducer, emptyQueue());
  const queueItems = useMemo(() => listQueue(queueState), [queueState]);
  const [chatState, chatDispatch] = useReducer(chatReducer, emptyChat());
  const chatSessions = useMemo(() => listChat(chatState), [chatState]);
  const [debugFrames, setDebugFrames] = useState<Frame[]>([]);
  const [liveTrace, setLiveTrace] = useState<TraceEvent[]>([]);
  const [highlightedEdge, setHighlightedEdge] = useState<string | null>(null);
  const [showDebug, setShowDebug] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [specsRows, setSpecsRows] = useState<PeerSpecsRow[]>([]);

  useEffect(() => {
    const stop = connectGatewayWS<Frame>({
      url: window.location.origin.replace(/^http/, "ws") + "/ws",
      onFrame: (frame) => {
        const frameType = (frame as { type?: string }).type;
        if (frameType === "queue.snapshot" || frameType === "queue.delta") {
          queueDispatch({ kind: "frame", frame: frame as unknown as QueueFrame });
          setDebugFrames((prev) => [...prev.slice(-499), frame]);
          return;
        }
        if (frameType === "chat.snapshot" || frameType === "chat.delta") {
          chatDispatch({ kind: "frame", frame: frame as unknown as ChatFrame });
          setDebugFrames((prev) => [...prev.slice(-499), frame]);
          return;
        }
        if (frameType === "alert") {
          alertsDispatch({ kind: "frame", frame: frame as unknown as AlertFrame });
          setDebugFrames((prev) => [...prev.slice(-499), frame]);
          return;
        }
        dispatch(frame);
        setDebugFrames((prev) => [...prev.slice(-499), frame]);
        if (frame.type === "message_trace") {
          const f = frame as unknown as Record<string, unknown>;
          setLiveTrace((prev) => [
            ...prev.slice(-499),
            {
              timestamp_ms: Number(f.timestamp_ms ?? 0),
              node_name: String(f.node_name ?? ""),
              event_type: String(f.event_type ?? ""),
              seq: typeof f.seq === "number" ? f.seq : undefined,
              duration_ms:
                typeof f.duration_ms === "number" ? f.duration_ms : null,
              error: (f.error as string | null | undefined) ?? null,
              payload: (f.payload as Record<string, unknown>) ?? {},
            },
          ]);
        }
      },
    });
    return stop;
  }, []);

  // Periodic re-render so quiet nodes fade to stale without a frame trigger.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), STALE_TICK_MS);
    return () => clearInterval(id);
  }, []);

  // Poll /peers so every mesh member shows up immediately on load and stays
  // marked alive while NATS presence sees them, even when there's no trace
  // activity flowing through this node's ring buffer.
  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const res = await fetch("/peers", { credentials: "same-origin" });
        if (!res.ok) return;
        const body = (await res.json()) as {
          peers?: Array<{
            node_id?: string;
            node_name?: string;
            self?: boolean;
            specs?: PeerSpecsRow["specs"];
            stale?: boolean;
          }>;
        };
        if (cancelled || !body.peers) return;
        const rows: PeerSpecsRow[] = [];
        for (const p of body.peers) {
          if (!p.node_name) continue;
          dispatch({ type: "hello", node_name: p.node_name, uptime_s: 0 });
          rows.push({
            node_id: p.node_id ?? p.node_name,
            node_name: p.node_name,
            self: Boolean(p.self),
            specs: p.specs ?? null,
            stale: Boolean(p.stale),
          });
        }
        setSpecsRows(rows);
      } catch {
        // best-effort; the WS path also feeds the graph
      }
    }
    poll();
    const id = setInterval(poll, PEERS_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);
  const visibleState = useMemo(() => markStale(state, now), [state, now]);

  // Backtick toggles the debug pane (hidden by default).
  useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      if (ev.key === "~" || ev.key === "`") {
        setShowDebug((s) => !s);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Selecting a trace event briefly highlights the matching call-graph edge.
  function onTraceSelect(e: TraceEvent) {
    const stream = e.event_type.replace(/\.(start|end|error)$/, "");
    const edgeId = `${e.node_name}::${stream}::${e.seq ?? ""}`;
    setHighlightedEdge(edgeId);
    setTimeout(() => setHighlightedEdge(null), HIGHLIGHT_MS);
  }

  return (
    <div className="flex h-full flex-col">
      <AlertBanner alerts={alerts} />
      <header className="border-b border-neutral-800 px-4 py-2 text-sm font-semibold">
        turing — fleet observability
        <span className="ml-2 text-xs font-normal text-neutral-500">
          press ` to toggle debug stream
        </span>
      </header>
      <section className="h-[42%] min-h-[260px] border-b border-neutral-800">
        <QueuePane items={queueItems} />
      </section>
      <main className="flex flex-1 overflow-hidden">
        <section className="flex flex-1 flex-col border-r border-neutral-800">
          <div className="flex-1 overflow-hidden">
            <CallGraphCanvas
              state={visibleState}
              highlightedEdge={highlightedEdge}
            />
          </div>
          <SpecsGrid rows={specsRows} />
        </section>
        <aside className="w-[420px] border-r border-neutral-800">
          <TracePane liveEvents={liveTrace} onSelect={onTraceSelect} />
        </aside>
        <aside className="w-[360px] border-r border-neutral-800">
          <ChatPane sessions={chatSessions} />
        </aside>
        {showDebug && (
          <aside className="w-[480px] overflow-auto bg-neutral-950 p-2 font-mono text-xs">
            <h2 className="mb-2 text-neutral-400">incoming frames (debug)</h2>
            <ol className="space-y-1">
              {debugFrames.map((f, i) => (
                <li
                  key={i}
                  className="rounded border border-neutral-800 bg-neutral-900 p-2 text-neutral-300"
                >
                  <pre className="whitespace-pre-wrap break-words">
                    {JSON.stringify(f, null, 2)}
                  </pre>
                </li>
              ))}
            </ol>
          </aside>
        )}
      </main>
    </div>
  );
}
