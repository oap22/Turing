// The Hyprland/omarchy-style tiling shell rendered in place of the browser
// tab UI when `isTauri()`. Owns the `LayoutState` (persisted layout across
// restarts), the global keymap, and the pane chrome; every gateway-backed
// pane below renders the *same* component the browser tab UI uses, just fed
// from `surface` instead of App's own hooks.

import { useEffect, useReducer, useRef, useState } from "react";
import type { ChatSession, QueueItem } from "../ws";
import type { GraphState } from "../graph/types";
import type { PeerSpecsRow } from "../specs/types";
import type { TraceEvent } from "../trace/types";
import QueuePane from "../queue/QueuePane";
import ChatPane from "../chat/ChatPane";
import ObservabilityView from "../ObservabilityView";
import { WS_STATUS_LABEL, type WsStatus } from "../App";
import Cheatsheet from "./Cheatsheet";
import Launcher from "./Launcher";
import {
  actionFor,
  movesFocus,
  PANE_FOCUS_EVENT,
  type Action,
  type PaneFocusDetail,
} from "./keymap";
import {
  closeFocused,
  closeLeafById,
  defaultLayout,
  focusEffect,
  focusLeaf,
  moveFocus,
  openPane,
  rects,
  resize,
  sendToWs,
  seedIds,
  swap,
  switchWs,
  toggleDir,
  toggleZoom,
  type FocusSource,
  type LayoutState,
  type Leaf,
  type Node,
  type PaneType,
  type Rect,
  type Split,
} from "./layout";
import { applyGaps, GAPS_IN_PX, GAPS_OUT_PX } from "./gaps";
import Home from "./Home";
import {
  activeSession,
  createSession,
  deleteSession,
  listSessions,
  loadSessions,
  renameSession,
  saveLayoutInto,
  saveSessions,
  switchSession,
  type SessionStore,
} from "./sessions";
import AgentFeedPane from "./panes/AgentFeedPane";
import AgentsPane from "./panes/AgentsPane";
import FlywheelPane from "./panes/FlywheelPane";
import ImagesPane from "./panes/ImagesPane";
import MetricsPane from "./panes/MetricsPane";
import TermPane from "./panes/TermPane";
import { inv, isTauri, subscribe } from "./tauri";
import { LAYOUT_FILE_REL, applyLayoutRequest } from "./layoutRequest";
import { applyTheme, initTheme, THEMES } from "./theme";

const TITLE_ROW_PX = 20;
const TOP_BAR_PX = 24;
const SPLITTER_HIT_PX = 4;

export interface Surface {
  queue: { items: QueueItem[] };
  chat: { sessions: ChatSession[] };
  obs: {
    graphState: GraphState;
    highlightedEdge: string | null;
    specsRows: PeerSpecsRow[];
    liveTrace: TraceEvent[];
    onTraceSelect: (e: TraceEvent) => void;
  };
  wsStatus: WsStatus;
}

interface Props {
  surface: Surface;
}

type ShellAction =
  | { type: "keymap"; action: Action }
  | { type: "openPane"; pane: PaneType; params?: Record<string, unknown> }
  | { type: "setRoot"; ws: number; root: Node }
  | { type: "closeLeaf"; ws: number; leafId: string }
  | { type: "focusLeaf"; leafId: string }
  | { type: "setLayout"; layout: LayoutState }
  | { type: "layoutRequest"; req: unknown };

function layoutReducer(state: LayoutState, action: ShellAction): LayoutState {
  if (action.type === "setLayout") {
    // Wholesale replacement — restoring a saved session. Ids in the incoming
    // tree were minted by whichever run created that session, so seed past
    // them before it can collide with the next `nextId()`.
    seedIds(action.layout);
    return action.layout;
  }
  if (action.type === "layoutRequest") {
    // Applied inside the reducer so it always compiles against the live
    // layout, never a state captured when the watcher subscribed. A request
    // that is malformed, out of range, or asks for nothing returns null and
    // the layout is kept exactly as it was.
    //
    // The catch is not decoration. This runs in React's render phase, so it
    // is outside the watcher's own try/catch, and there is no error boundary
    // above it — anything thrown here unmounts the whole shell, and since the
    // offending file is still on disk it would do it again on every relaunch.
    // `applyLayoutRequest` caps depth and size to keep that unreachable; this
    // is the second lock on the same door.
    try {
      return applyLayoutRequest(state, action.req) ?? state;
    } catch {
      return state;
    }
  }
  if (action.type === "setRoot") {
    const workspaces = state.workspaces.slice();
    workspaces[action.ws] = { ...workspaces[action.ws], root: action.root };
    return { ...state, workspaces };
  }
  if (action.type === "openPane") {
    return openPane(state, action.pane, action.params);
  }
  if (action.type === "focusLeaf") {
    return focusLeaf(state, action.leafId);
  }
  if (action.type === "closeLeaf") {
    // Closes THAT pane's title-row × button targets, which may not be the
    // focused pane (and may be in a non-active workspace, though in
    // practice only active-workspace panes render an × to click).
    return closeLeafById(state, action.ws, action.leafId);
  }
  const a = action.action;
  switch (a.type) {
    case "newTerm":
      return openPane(state, "term");
    case "close":
      return closeFocused(state);
    case "focus":
      return moveFocus(state, a.dir);
    case "swap":
      return swap(state, a.dir);
    case "ws":
      return switchWs(state, a.i);
    case "sendWs":
      return sendToWs(state, a.i);
    case "zoom":
      return toggleZoom(state);
    case "toggleDir":
      return toggleDir(state);
    case "resize":
      return resize(state, a.delta);
    default:
      return state;
  }
}

// The layout to boot with: the last-active saved session's, or the
// first-launch preset when there is nothing saved at all. `sessions.ts` has
// already done the legacy-blob migration by the time the store gets here, so
// an existing user's single persisted layout arrives as a session named
// "default" and they come straight up on it, exactly as before.
function initLayout(store: SessionStore): LayoutState {
  const current = activeSession(store);
  if (current) {
    // Restored leaf ids were minted by a previous module instance; bump the
    // module-local id counter past them so the next `nextId()` call (e.g.
    // ⌘Return for a new terminal) can't collide with an id already in the
    // tree. `defaultLayout()` mints its own fresh ids and needs no seeding.
    seedIds(current.layout);
    return current.layout;
  }
  return defaultLayout();
}

function collectLeaves(node: Node, out: Leaf[]): void {
  if (node.kind === "leaf") {
    out.push(node);
    return;
  }
  collectLeaves(node.a, out);
  collectLeaves(node.b, out);
}

interface Boundary {
  split: Split;
  axis: "h" | "v";
  at: number;
  from: number;
  to: number;
}

function collectBoundaries(node: Node, rect: Rect, out: Boundary[]): void {
  if (node.kind === "leaf") return;
  if (node.dir === "h") {
    const boundaryX = rect.x + rect.w * node.ratio;
    out.push({
      split: node,
      axis: "h",
      at: boundaryX,
      from: rect.y,
      to: rect.y + rect.h,
    });
    collectBoundaries(
      node.a,
      { x: rect.x, y: rect.y, w: rect.w * node.ratio, h: rect.h },
      out,
    );
    collectBoundaries(
      node.b,
      {
        x: rect.x + rect.w * node.ratio,
        y: rect.y,
        w: rect.w * (1 - node.ratio),
        h: rect.h,
      },
      out,
    );
  } else {
    const boundaryY = rect.y + rect.h * node.ratio;
    out.push({
      split: node,
      axis: "v",
      at: boundaryY,
      from: rect.x,
      to: rect.x + rect.w,
    });
    collectBoundaries(
      node.a,
      { x: rect.x, y: rect.y, w: rect.w, h: rect.h * node.ratio },
      out,
    );
    collectBoundaries(
      node.b,
      {
        x: rect.x,
        y: rect.y + rect.h * node.ratio,
        w: rect.w,
        h: rect.h * (1 - node.ratio),
      },
      out,
    );
  }
}

function withRatioAt(node: Node, target: Split, ratio: number): Node {
  if (node === target) return { ...node, ratio };
  if (node.kind === "leaf") return node;
  return {
    ...node,
    a: withRatioAt(node.a, target, ratio),
    b: withRatioAt(node.b, target, ratio),
  };
}

function paneLabel(leaf: Leaf): string {
  const runnerId = leaf.params?.runnerId;
  return typeof runnerId === "string"
    ? `${leaf.pane} — ${runnerId}`
    : leaf.pane;
}

// Move the OS pointer to the centre of a pane. JS cannot move the cursor, so
// this hands window-content coordinates (CSS px, exactly what
// getBoundingClientRect returns) to Rust, which converts them to the
// screen-relative position the platform API wants.
//
// Best-effort by design: a no-op in a plain browser, and errors are
// swallowed. If the platform ever refuses the move, focus-follow still works
// and the pointer simply stays put — a degraded feature, not a broken app.
function warpCursorToCenter(el: HTMLElement): void {
  if (!isTauri()) return;
  const r = el.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return;
  void inv("warp_cursor", {
    x: r.left + r.width / 2,
    y: r.top + r.height / 2,
  }).catch(() => {});
}

function PaneBody({
  leaf,
  visible,
  surface,
}: {
  leaf: Leaf;
  visible: boolean;
  surface: Surface;
}) {
  switch (leaf.pane) {
    case "term":
      return (
        <TermPane
          leafId={leaf.id}
          runnerId={
            typeof leaf.params?.runnerId === "string"
              ? leaf.params.runnerId
              : undefined
          }
          visible={visible}
        />
      );
    case "queue":
      return <QueuePane items={surface.queue.items} />;
    case "chat":
      return <ChatPane sessions={surface.chat.sessions} />;
    case "obs":
      return (
        <ObservabilityView
          graphState={surface.obs.graphState}
          highlightedEdge={surface.obs.highlightedEdge}
          specsRows={surface.obs.specsRows}
          liveTrace={surface.obs.liveTrace}
          onTraceSelect={surface.obs.onTraceSelect}
        />
      );
    case "metrics":
      return <MetricsPane />;
    case "images":
      return <ImagesPane />;
    case "flywheel":
      return <FlywheelPane />;
    case "agents":
      return <AgentsPane />;
    case "agentfeed":
      return <AgentFeedPane />;
    default:
      return null;
  }
}

export default function DesktopShell({ surface }: Props) {
  // Read storage exactly once, before the reducer, and hand the same store to
  // both: the reducer needs the active session's layout to boot with, and the
  // picker below needs to know how many sessions there are.
  const [sessions, setSessions] = useState<SessionStore>(() =>
    loadSessions(localStorage),
  );
  const [state, dispatch] = useReducer(layoutReducer, sessions, initLayout);
  // The app opens on Home — always, not just when there is more than one saved
  // workstation. It is the only surface that lists what you have and the only
  // place a finished workstation can be torn down without entering it first, so
  // hiding it from single-workstation users hid the feature from exactly the
  // people who had never discovered it. Escape (or Return on the pre-selected
  // last-used row) is one keystroke back to where they left off.
  const [home, setHome] = useState(true);
  const [overlay, setOverlay] = useState<"launcher" | "cheatsheet" | null>(null);
  const [theme, setTheme] = useState(() => localStorage.getItem("turing.theme") ?? "turing");
  const [now, setNow] = useState(() => new Date());
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [viewport, setViewport] = useState<Rect>({
    x: 0,
    y: 0,
    w: 1600,
    h: 900,
  });

  // Hyprland-style focus-follow. `layoutReducer` is pure and can't know what
  // provoked a transition, so the provocation is recorded here at the
  // dispatch site and consumed once by the effect below. It resets to
  // "passive" every time, so a render triggered by anything else (persistence,
  // viewport resize, surface props) can never move focus or the pointer.
  const focusSourceRef = useRef<FocusSource>("passive");
  const prevStateRef = useRef<LayoutState>(state);
  // Read inside the effect rather than closed over, so the guard sees the
  // overlay state as of the commit.
  const overlayRef = useRef(overlay);
  overlayRef.current = overlay;

  function dispatchFrom(action: ShellAction, source: FocusSource) {
    focusSourceRef.current = source;
    dispatch(action);
  }

  useEffect(() => {
    const prev = prevStateRef.current;
    prevStateRef.current = state;
    // Consume the source exactly once, whatever happens next.
    const source = focusSourceRef.current;
    focusSourceRef.current = "passive";

    // An open Launcher/Cheatsheet owns the keyboard; pulling focus into a
    // pane underneath would break typing in the overlay's own input.
    if (overlayRef.current) return;

    const effect = focusEffect(prev, state, source);
    if (!effect) return;

    const el = containerRef.current?.querySelector<HTMLElement>(
      `[data-leaf-id="${CSS.escape(effect.leafId)}"]`,
    );
    if (!el) return;

    if (el.dataset.paneType === "term") {
      // xterm's focusable element is its own hidden textarea; only TermPane
      // can reach it, so ask rather than reaching in.
      window.dispatchEvent(
        new CustomEvent<PaneFocusDetail>(PANE_FOCUS_EVENT, {
          detail: { leafId: effect.leafId },
        }),
      );
    } else {
      // preventScroll: the pane is already positioned absolutely at its tile;
      // letting the browser scroll it into view would shift the whole grid.
      el.querySelector<HTMLElement>("[data-pane-body]")?.focus({
        preventScroll: true,
      });
    }

    if (effect.warp) warpCursorToCenter(el);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state]);

  useEffect(() => {
    initTheme();
  }, []);

  // Autosave. Every layout edit folds into the *currently active* session
  // rather than a global blob, which is the whole point of the feature: park
  // "bench", open "triage", and neither one leaks into the other. Depending on
  // `state` alone is deliberate — the effect body is re-created every render,
  // so it always closes over the newest store, and adding `sessions` to the
  // deps would re-run it on the very `setSessions` it just performed.
  useEffect(() => {
    // Nothing is on screen while Home is up, and writing then would bump the
    // last-used workstation's timestamp and re-order the very list the user is
    // arrowing through — or, worse, re-create a workstation they just removed.
    if (home) return;
    const next = saveLayoutInto(sessions, state);
    setSessions(next);
    saveSessions(localStorage, next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state, home]);

  function commitSessions(next: SessionStore) {
    setSessions(next);
    saveSessions(localStorage, next);
  }

  // Load a session's layout into the shell. The outgoing layout needs no
  // explicit save — autosave has already written every edit into the session
  // being left. Panes mount fresh: a restored session re-opens the same shape,
  // not the same processes.
  function goToSession(id: string) {
    const next = switchSession(sessions, id);
    const target = next.sessions[id];
    if (!target) return;
    commitSessions(next);
    dispatchFrom({ type: "setLayout", layout: target.layout }, "passive");
    setHome(false);
  }

  // Home's "new": a fresh workstation on the first-launch preset, saved under
  // the given name and entered immediately. Distinct from ⌘p's "save layout
  // as…", which snapshots whatever is already on screen.
  function createWorkstation(name: string) {
    const layout = defaultLayout();
    const next = createSession(sessions, name, layout);
    commitSessions(next);
    dispatchFrom({ type: "setLayout", layout }, "passive");
    setHome(false);
  }

  function renameWorkstation(id: string, name: string) {
    commitSessions(renameSession(sessions, id, name));
  }

  // Removing from Home never has to touch the live layout: the shell is not
  // mounted, and whichever workstation is opened next replaces `state`
  // wholesale. `deleteSession` re-points `activeId` on its own, so removing the
  // last-used one leaves Escape pointing at the next most recent.
  function removeWorkstation(id: string) {
    commitSessions(deleteSession(sessions, id));
  }

  // Escape on Home: resume the last-used workstation. With nothing saved at all
  // there is nothing to resume, so fall through to the first-launch preset —
  // autosave writes it back as a fresh "default" the moment the shell mounts.
  function resumeLastUsed() {
    const current = activeSession(sessions);
    if (current) {
      goToSession(current.id);
      return;
    }
    dispatchFrom({ type: "setLayout", layout: defaultLayout() }, "passive");
    setHome(false);
  }

  // Snapshot the live layout under a new name and make it current, so further
  // edits keep landing in the session the user just named.
  function saveSessionAs(name: string) {
    commitSessions(createSession(sessions, name, state));
  }

  function renameActiveSession(name: string) {
    const current = activeSession(sessions);
    if (!current) return;
    commitSessions(renameSession(sessions, current.id, name));
  }

  // Deleting the session you are in leaves you somewhere: the next most
  // recently used session, or the first-launch preset if that was the last
  // one (which autosave immediately re-saves as a fresh "default").
  function deleteActiveSession() {
    const current = activeSession(sessions);
    if (!current) return;
    const next = deleteSession(sessions, current.id);
    commitSessions(next);
    const target = activeSession(next);
    dispatchFrom(
      { type: "setLayout", layout: target?.layout ?? defaultLayout() },
      "passive",
    );
  }

  // Agent-driven pane control (#388). Watches `.layout.json` under the
  // results root, the same way the metrics pane watches `.viewer.json`, and
  // rearranges the workspaces live — no relaunch, and no hand-editing the
  // persisted layout blob, which is how this had to be done before.
  //
  // Dispatched "passive" on purpose: an agent rearranging panes must never
  // pull DOM focus out of whatever Owen is typing in, and must never warp the
  // cursor. `focusEffect` returns null for passive, so neither can happen.
  useEffect(() => {
    if (!isTauri()) return;
    let cancelled = false;

    async function loadLayoutFile() {
      try {
        const text = await inv<string>("fs_read_text", {
          root: "results",
          rel: LAYOUT_FILE_REL,
        });
        if (cancelled) return;
        dispatchFrom(
          { type: "layoutRequest", req: JSON.parse(text) },
          "passive",
        );
      } catch {
        // Absent, unreadable, or mid-write and not yet valid JSON — nothing
        // to apply. The next fs-change brings the finished file.
      }
    }

    const sub = subscribe<{ root: string; rel_path: string }>(
      "fs-change",
      (payload) => {
        if (cancelled || payload.root !== "results") return;
        if (payload.rel_path === LAYOUT_FILE_REL) void loadLayoutFile();
      },
    );

    // Order matters: await `sub.ready` before asking the backend to watch, or
    // a change landing between `fs_watch` returning and the Tauri listener
    // attaching is dropped. It would self-heal on the next write, but a
    // control file written once at startup would appear to be ignored.
    void sub.ready
      .then(() => inv("fs_watch", { root: "results", rel: "" }))
      .catch(() => {
        // Results root missing on this machine: no control file to watch.
      })
      .then(() => {
        if (!cancelled) void loadLayoutFile();
      });

    return () => {
      cancelled = true;
      sub.unsubscribe();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 30_000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const r = entries[0]?.contentRect;
      if (r) setViewport({ x: 0, y: 0, w: r.width, h: r.height });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      // Home owns the keyboard outright — the shell these actions would act on
      // isn't even mounted yet.
      if (home) return;
      if (overlay) {
        if (e.key === "Escape") {
          setOverlay(null);
          e.preventDefault();
          e.stopPropagation();
          return;
        }
        const action = actionFor(e);
        if (action?.type === "ws") {
          dispatchFrom({ type: "keymap", action }, "keyboard");
          e.preventDefault();
          e.stopPropagation();
        }
        return;
      }
      const action = actionFor(e);
      if (!action) return;
      e.preventDefault();
      e.stopPropagation();
      if (action.type === "launcher") {
        setOverlay("launcher");
        return;
      }
      if (action.type === "cheatsheet") {
        setOverlay("cheatsheet");
        return;
      }
      if (action.type === "home") {
        // The layout is already saved — autosave ran on the last edit — so
        // going home is just a screen change, and coming back re-mounts the
        // panes with fresh shells.
        setOverlay(null);
        setHome(true);
        return;
      }
      dispatchFrom({ type: "keymap", action }, movesFocus(action) ? "keyboard" : "passive");
    }
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [overlay, home]);

  const sessionList = listSessions(sessions);
  const current = activeSession(sessions);

  // Rendered *instead of* the shell, not over it: mounting the pane tree
  // underneath would spawn PTYs for a layout the user is about to replace.
  if (home) {
    return (
      <Home
        sessions={sessionList}
        activeId={current?.id ?? null}
        onOpen={goToSession}
        onCreate={createWorkstation}
        onRename={renameWorkstation}
        onRemove={removeWorkstation}
        onResume={resumeLastUsed}
        theme={theme}
        onThemeChange={(id) => {
          applyTheme(id);
          setTheme(id);
        }}
      />
    );
  }

  const ws = state.workspaces[state.active];
  // The tiling area is the viewport minus the outer gap; everything
  // positional (leaf rects, splitter boundaries, drag spans) is measured
  // against it rather than the raw viewport. See `gaps.ts` for why the gaps
  // live here and not in the layout math.
  const tiled = applyGaps(viewport, GAPS_OUT_PX);
  const rectMap = ws.root ? rects(ws.root, tiled) : new Map<string, Rect>();
  const boundaries: Boundary[] = [];
  if (ws.root) collectBoundaries(ws.root, tiled, boundaries);

  function startDrag(b: Boundary, downEvent: React.PointerEvent) {
    downEvent.preventDefault();
    const startRatio = b.split.ratio;
    const startCoord = b.axis === "h" ? downEvent.clientX : downEvent.clientY;
    const span = b.axis === "h" ? tiled.w : tiled.h;
    function onMove(e: PointerEvent) {
      const coord = b.axis === "h" ? e.clientX : e.clientY;
      const delta = (coord - startCoord) / span;
      const newRatio = Math.min(0.9, Math.max(0.1, startRatio + delta));
      if (!ws.root) return;
      const newRoot = withRatioAt(ws.root, b.split, newRatio);
      dispatch({ type: "setRoot", ws: state.active, root: newRoot });
    }
    function onUp() {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    }
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  const allLeaves: Array<{ ws: number; leaf: Leaf }> = [];
  state.workspaces.forEach((w, i) => {
    if (!w.root) return;
    const leaves: Leaf[] = [];
    collectLeaves(w.root, leaves);
    for (const l of leaves) allLeaves.push({ ws: i, leaf: l });
  });

  const wsStatusInfo = WS_STATUS_LABEL[surface.wsStatus];
  const clock = `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;

  return (
    <div className="flex h-full flex-col bg-term-bg text-term-fg">
      <header
        style={{ height: TOP_BAR_PX }}
        className="flex shrink-0 items-center gap-3 border-b border-term-edge bg-term-panel px-3 text-[11px]"
      >
        {state.workspaces.map((w, i) => (
          <button
            key={i}
            type="button"
            onClick={() =>
              dispatchFrom(
                { type: "keymap", action: { type: "ws", i } },
                "pointer",
              )
            }
            className={`flex items-center gap-1 px-1.5 ${
              i === state.active ? "text-term-accent" : "text-term-dim"
            }`}
          >
            {i + 1}
            <span aria-hidden="true">{w.root ? "●" : "○"}</span>
          </button>
        ))}
        <span className={`ml-2 ${wsStatusInfo.cls}`}>
          {wsStatusInfo.dot} {wsStatusInfo.text}
        </span>
        {/* Which saved workstation these panes belong to — without it, "⌘p →
            session: rename" is aimed at something invisible. Clicking it goes
            back to Home, so the way out is where the name already is. */}
        <button
          type="button"
          onClick={() => setHome(true)}
          title="workstations (⌘0)"
          className="text-term-dim hover:text-term-accent"
        >
          {current ? `⌂ ${current.name}` : "⌂ workstations"}
        </button>
        <select
          value={theme}
          onChange={(e) => {
            applyTheme(e.target.value);
            setTheme(e.target.value);
          }}
          className="ml-auto border border-term-edge bg-term-bg text-term-fg"
        >
          {THEMES.map((t) => (
            <option key={t.id} value={t.id}>
              {t.label}
            </option>
          ))}
        </select>
        <span className="tabular-nums text-term-dim">{clock}</span>
      </header>
      <div
        ref={containerRef}
        className="relative min-h-0 flex-1 overflow-hidden"
      >
        {allLeaves.map(({ ws: wsIdx, leaf }) => {
          const isActiveWs = wsIdx === state.active;
          const isZoomedOut =
            state.workspaces[wsIdx].zoom &&
            leaf.id !== state.workspaces[wsIdx].focus;
          const visible = isActiveWs && !isZoomedOut;
          // Half the inner gap per pane: two neighbours each give up half, so
          // the visible seam between them is exactly GAPS_IN_PX. A zoomed (or
          // inactive-workspace) pane fills the tiling area, which is already
          // inset from the window edge by GAPS_OUT_PX.
          const rect = applyGaps(
            isActiveWs
              ? state.workspaces[wsIdx].zoom
                ? tiled
                : (rectMap.get(leaf.id) ?? tiled)
              : tiled,
            GAPS_IN_PX / 2,
          );
          const focused =
            isActiveWs && state.workspaces[wsIdx].focus === leaf.id;
          return (
            <div
              key={leaf.id}
              data-leaf-id={leaf.id}
              data-pane-type={leaf.pane}
              // Click coherence: pointing at a pane focuses it, so the accent
              // border follows the mouse as well as the keyboard. Pointer-down
              // (not click) so focus lands before any inner control reacts,
              // and "pointer" source so this never warps the cursor — it is
              // already exactly where the user put it.
              onPointerDown={() => {
                if (!isActiveWs || focused) return;
                dispatchFrom({ type: "focusLeaf", leafId: leaf.id }, "pointer");
              }}
              style={{
                position: "absolute",
                left: rect.x,
                top: rect.y,
                width: rect.w,
                height: rect.h,
                display: visible ? "flex" : "none",
                flexDirection: "column",
              }}
              className={`border ${focused ? "border-term-accent" : "border-term-edge"}`}
            >
              <div
                style={{ height: TITLE_ROW_PX }}
                className="flex shrink-0 items-center gap-2 border-b border-term-edge px-2 text-[10px] lowercase text-term-dim"
              >
                <span className="min-w-0 flex-1 truncate">
                  {paneLabel(leaf)}
                </span>
                <button
                  type="button"
                  aria-label={`close ${paneLabel(leaf)}`}
                  onClick={(e) => {
                    e.stopPropagation();
                    dispatchFrom(
                      { type: "closeLeaf", ws: wsIdx, leafId: leaf.id },
                      "pointer",
                    );
                  }}
                  className="shrink-0 px-1 leading-none text-term-dim hover:text-term-accent"
                >
                  ×
                </button>
              </div>
              {/* tabIndex -1 makes this programmatically focusable (but not
                  a Tab stop), so a non-term pane can take real keyboard
                  focus and be scrolled/keyed without a click. */}
              <div
                className="min-h-0 flex-1 outline-none"
                data-pane-body
                tabIndex={-1}
              >
                <PaneBody leaf={leaf} visible={visible} surface={surface} />
              </div>
            </div>
          );
        })}
        {!ws.zoom &&
          boundaries.map((b, i) => (
            <div
              key={i}
              onPointerDown={(e) => startDrag(b, e)}
              style={
                b.axis === "h"
                  ? {
                      position: "absolute",
                      left: b.at - SPLITTER_HIT_PX / 2,
                      top: b.from,
                      width: SPLITTER_HIT_PX,
                      height: b.to - b.from,
                      cursor: "col-resize",
                    }
                  : {
                      position: "absolute",
                      left: b.from,
                      top: b.at - SPLITTER_HIT_PX / 2,
                      width: b.to - b.from,
                      height: SPLITTER_HIT_PX,
                      cursor: "row-resize",
                    }
              }
            />
          ))}
        {allLeaves.length === 0 && (
          <div className="flex h-full items-center justify-center text-term-dim">
            ⌘ Return for a terminal · ⌘ p for the launcher
          </div>
        )}
      </div>
      {overlay === "launcher" && (
        <Launcher
          onClose={() => setOverlay(null)}
          onOpenPane={(pane) =>
            dispatchFrom({ type: "openPane", pane }, "keyboard")
          }
          onOpenRunner={(runnerId) =>
            dispatchFrom(
              { type: "openPane", pane: "term", params: { runnerId } },
              "keyboard",
            )
          }
          sessions={sessionList}
          activeSession={current}
          onSaveSession={saveSessionAs}
          onRenameSession={renameActiveSession}
          onSwitchSession={goToSession}
          onDeleteSession={deleteActiveSession}
        />
      )}
      {overlay === "cheatsheet" && (
        <Cheatsheet onClose={() => setOverlay(null)} />
      )}
    </div>
  );
}
