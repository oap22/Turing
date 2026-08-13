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
import ChatPane from "./chat/ChatPane";
import {
  applyChatFrame,
  emptyChat,
  listChat,
  type ChatState,
} from "./chat/reducer";
import DesktopShell, { type Surface } from "./desktop/DesktopShell";
import { gatewayFetch } from "./desktop/gateway";
import { isTauri } from "./desktop/tauri";
import { emptyState, markStale, reduce, type Frame } from "./graph/reducer";
import ObservabilityView from "./ObservabilityView";
import QueuePane from "./queue/QueuePane";
import {
  applyQueueFrame,
  emptyQueue,
  listQueue,
  type QueueState,
} from "./queue/reducer";
import type { PeerSpecsRow } from "./specs/types";
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

export type WsStatus = "open" | "closed" | "reconnecting";

// Memoized panes: a frame for one surface (e.g. a trace event) no longer
// re-renders the others — each pane only re-renders when its own props change.
const MemoAlertBanner = memo(AlertBanner);
const MemoQueuePane = memo(QueuePane);
const MemoChatPane = memo(ChatPane);

// ── Tabbed views (issue #358) ────────────────────────────────────────────────
//
// One view at a time, grouped by operator activity: Queue (the hero surface,
// default), Chat, and Observability (graph + specs + trace). All reducers stay
// mounted in App, so hidden views keep ingesting WS frames and the badges stay
// live. The active view syncs to the URL hash so a reload lands where you were.

type TabId = "queue" | "chat" | "obs";

const TABS: ReadonlyArray<{ id: TabId; label: string; hotkey: string }> = [
  { id: "queue", label: "queue", hotkey: "1" },
  { id: "chat", label: "chat", hotkey: "2" },
  { id: "obs", label: "observability", hotkey: "3" },
];

function tabFromHash(hash: string): TabId | null {
  const id = hash.replace(/^#/, "");
  return TABS.find((t) => t.id === id)?.id ?? null;
}

export const WS_STATUS_LABEL: Record<WsStatus, { dot: string; text: string; cls: string }> = {
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
  const [tab, setTab] = useState<TabId>(
    () => tabFromHash(window.location.hash) ?? "queue",
  );

  const navigate = useCallback((next: TabId) => {
    setTab(next);
    // replaceState keeps the hash bookmarkable without spamming history or
    // re-firing hashchange.
    if (window.location.hash !== `#${next}`) {
      window.history.replaceState(null, "", `#${next}`);
    }
  }, []);

  // External hash edits (or back/forward) still steer the view. Non-tab
  // hashes (the #operator-surface skip link, in-page anchors) are ignored
  // rather than coerced, so following them never switches the view.
  useEffect(() => {
    const initial = window.location.hash;
    if (initial !== "" && tabFromHash(initial) === null) {
      window.history.replaceState(null, "", "#queue");
    }
    const onHash = () => {
      const next = tabFromHash(window.location.hash);
      if (next !== null) setTab(next);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

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
        const res = await gatewayFetch("/peers");
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

  // Backtick toggles the debug pane (hidden by default); 1/2/3 switch views.
  // Both are suppressed while typing in a form control.
  useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      const target = ev.target instanceof HTMLElement ? ev.target : null;
      if (
        target?.closest("input, textarea, select") ||
        target?.isContentEditable ||
        ev.metaKey ||
        ev.ctrlKey ||
        ev.altKey
      ) {
        return;
      }
      const hot = TABS.find((t) => t.hotkey === ev.key);
      if (hot) {
        navigate(hot.id);
        return;
      }
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
  }, [navigate]);

  // Tab badges mean "you owe a decision", not "something happened": proposed
  // items await approval, drafted items await curation, completed subtasks
  // await a thumb. Observability carries no badge — alerts own the banner.
  const queueBadge = useMemo(
    () =>
      queueItems.filter(
        (i) => i.status === "proposed" || i.status === "drafted",
      ).length,
    [queueItems],
  );
  const chatBadge = useMemo(
    () =>
      chatSessions.reduce(
        (n, s) =>
          n + s.subtasks.filter((st) => st.status === "completed").length,
        0,
      ),
    [chatSessions],
  );
  const badges = useMemo<Record<TabId, number>>(
    () => ({ queue: queueBadge, chat: chatBadge, obs: 0 }),
    [queueBadge, chatBadge],
  );

  // Selecting a trace event briefly highlights the matching call-graph edge.
  const onTraceSelect = useCallback((e: TraceEvent) => {
    const stream = e.event_type.replace(/\.(start|end|error)$/, "");
    const edgeId = `${e.node_name}::${stream}::${e.seq ?? ""}`;
    setHighlightedEdge(edgeId);
    setTimeout(() => setHighlightedEdge(null), HIGHLIGHT_MS);
  }, []);

  const ws = WS_STATUS_LABEL[wsStatus];

  // Bundles the props the three gateway-backed views already receive below,
  // so the desktop shell's tiling panes can render the exact same components
  // outside the tabbed layout (issue #382).
  const surface: Surface = {
    queue: { items: queueItems },
    chat: { sessions: chatSessions },
    obs: {
      graphState: visibleState,
      highlightedEdge,
      specsRows,
      liveTrace,
      onTraceSelect,
    },
    wsStatus,
  };

  if (isTauri()) {
    return <DesktopShell surface={surface} />;
  }

  return (
    <div className="flex h-full flex-col bg-term-bg text-term-fg">
      <a
        href="#operator-surface"
        className="sr-only focus:not-sr-only focus:absolute focus:left-3 focus:top-3 focus:z-[60] focus:rounded focus:bg-term-panel focus:px-3 focus:py-2 focus:text-sm focus:text-term-fg"
      >
        skip to operator surface
      </a>
      <MemoAlertBanner alerts={alerts} />
      <header className="flex items-center gap-3 border-b border-term-edge bg-term-panel px-3 py-1.5 text-sm">
        <span className="bg-term-accent px-2 py-0.5 text-xs font-bold tracking-widest text-black">
          TURING
        </span>
        <nav aria-label="Views" className="flex items-center gap-1 text-xs">
          {TABS.map((t) => {
            const active = tab === t.id;
            const badge = badges[t.id];
            return (
              <button
                key={t.id}
                type="button"
                data-testid={`tab-${t.id}`}
                aria-current={active ? "page" : undefined}
                onClick={() => navigate(t.id)}
                className={`flex items-center gap-1.5 border px-2 py-0.5 uppercase tracking-wider ${
                  active
                    ? "border-term-accent text-term-accent"
                    : "border-transparent text-term-dim hover:text-term-fg"
                }`}
              >
                <kbd className="border border-term-edge bg-term-raised px-1 normal-case text-term-dim">
                  {t.hotkey}
                </kbd>
                {t.label}
                {badge > 0 && (
                  <span
                    data-testid={`tab-badge-${t.id}`}
                    aria-label={`${badge} items awaiting a decision`}
                    className="bg-term-raised px-1 tabular-nums text-amber-300"
                  >
                    {badge}
                  </span>
                )}
              </button>
            );
          })}
        </nav>
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
      <main id="operator-surface" className="flex min-h-0 flex-1 overflow-hidden">
        <div className="min-w-0 flex-1 overflow-hidden">
          {tab === "queue" && (
            <section aria-label="Question queue" className="h-full">
              <MemoQueuePane items={queueItems} />
            </section>
          )}
          {tab === "chat" && (
            <section aria-label="Chat tasks" className="h-full">
              <MemoChatPane sessions={chatSessions} />
            </section>
          )}
          {tab === "obs" && (
            <ObservabilityView
              graphState={visibleState}
              highlightedEdge={highlightedEdge}
              specsRows={specsRows}
              liveTrace={liveTrace}
              onTraceSelect={onTraceSelect}
            />
          )}
        </div>
        {showDebug && (
          <aside
            aria-label="Incoming frames debug stream"
            className="w-[480px] shrink-0 overflow-auto border-l border-term-edge bg-term-panel p-2 font-mono text-xs"
          >
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
