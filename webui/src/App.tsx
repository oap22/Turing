import {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from "react";
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
const DEBUG_RING_CAP = 500;

type WsStatus = "open" | "closed" | "reconnecting";

// Memoized panes: a frame for one surface (e.g. a trace event) no longer
// re-renders the others — each pane only re-renders when its own props change.
const MemoAlertBanner = memo(AlertBanner);
const MemoQueuePane = memo(QueuePane);
const MemoChatPane = memo(ChatPane);
const MemoTracePane = memo(TracePane);
const MemoSpecsGrid = memo(SpecsGrid);
const MemoCallGraphCanvas = memo(CallGraphCanvas);

const WS_STATUS_LABEL: Record<WsStatus, { dot: string; text: string; cls: string }> = {
  open: { dot: "●", text: "live", cls: "text-emerald-400" },
  reconnecting: { dot: "◌", text: "reconnecting", cls: "text-amber-400" },
  closed: { dot: "●", text: "offline", cls: "text-rose-400" },
};

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
  const [wsStatus, setWsStatus] = useState<WsStatus>("reconnecting");

  // The debug ring lives in a ref so recording a frame is free; it is only
  // mirrored into state (→ re-render) while the debug pane is actually open.
  const debugRing = useRef<Frame[]>([]);
  const showDebugRef = useRef(false);
  showDebugRef.current = showDebug;

  useEffect(() => {
    const stop = connectGatewayWS<Frame>({
      url: window.location.origin.replace(/^http/, "ws") + "/ws",
      onStatus: (status) => setWsStatus(status),
      onFrame: (frame) => {
        debugRing.current = [
          ...debugRing.current.slice(-(DEBUG_RING_CAP - 1)),
          frame,
        ];
        if (showDebugRef.current) setDebugFrames(debugRing.current);
        const frameType = (frame as { type?: string }).type;
        if (frameType === "queue.snapshot" || frameType === "queue.delta") {
          queueDispatch({ kind: "frame", frame: frame as unknown as QueueFrame });
          return;
        }
        if (frameType === "chat.snapshot" || frameType === "chat.delta") {
          chatDispatch({ kind: "frame", frame: frame as unknown as ChatFrame });
          return;
        }
        if (frameType === "alert") {
          alertsDispatch({ kind: "frame", frame: frame as unknown as AlertFrame });
          return;
        }
        dispatch(frame);
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
        setShowDebug((s) => {
          const next = !s;
          if (next) setDebugFrames(debugRing.current);
          return next;
        });
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Selecting a trace event briefly highlights the matching call-graph edge.
  const onTraceSelect = useCallback((e: TraceEvent) => {
    const stream = e.event_type.replace(/\.(start|end|error)$/, "");
    const edgeId = `${e.node_name}::${stream}::${e.seq ?? ""}`;
    setHighlightedEdge(edgeId);
    setTimeout(() => setHighlightedEdge(null), HIGHLIGHT_MS);
  }, []);

  const ws = WS_STATUS_LABEL[wsStatus];

  return (
    <div className="flex h-full flex-col bg-term-bg text-term-fg">
      <MemoAlertBanner alerts={alerts} />
      <header className="flex items-center gap-3 border-b border-term-edge bg-term-panel px-3 py-1.5 text-sm">
        <span className="bg-term-accent px-2 py-0.5 text-xs font-bold tracking-widest text-black">
          TURING
        </span>
        <span className="text-xs text-term-dim">// fleet console</span>
        <span className="ml-auto flex items-center gap-4 text-xs">
          <span className="text-term-dim">
            <kbd className="border border-term-edge bg-term-raised px-1 text-term-fg">
              `
            </kbd>{" "}
            debug
          </span>
          <span data-testid="ws-status" data-status={wsStatus} className={ws.cls}>
            {ws.dot} {ws.text}
          </span>
        </span>
      </header>
      <section className="h-[42%] min-h-[260px] border-b border-term-edge">
        <MemoQueuePane items={queueItems} />
      </section>
      <main className="flex flex-1 overflow-hidden">
        <section className="flex min-w-0 flex-1 flex-col border-r border-term-edge">
          <div className="flex-1 overflow-hidden">
            <MemoCallGraphCanvas
              state={visibleState}
              highlightedEdge={highlightedEdge}
            />
          </div>
          <MemoSpecsGrid rows={specsRows} />
        </section>
        <aside className="w-[420px] shrink-0 border-r border-term-edge">
          <MemoTracePane liveEvents={liveTrace} onSelect={onTraceSelect} />
        </aside>
        <aside className="w-[360px] shrink-0">
          <MemoChatPane sessions={chatSessions} />
        </aside>
        {showDebug && (
          <aside className="w-[480px] shrink-0 overflow-auto border-l border-term-edge bg-term-panel p-2 font-mono text-xs">
            <h2 className="mb-2 text-[11px] uppercase tracking-widest text-term-dim">
              incoming frames<span className="term-cursor" />
            </h2>
            <ol className="space-y-1">
              {debugFrames.map((f, i) => (
                <li
                  key={i}
                  className="border border-term-edge bg-term-raised p-2 text-neutral-300"
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
