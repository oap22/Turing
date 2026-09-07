// Named, saved layouts ("sessions"). The shell used to persist exactly one
// layout — a single `localStorage["turing.layout.v2"]` blob rewritten on every
// change — so the only thing you could ever come back to was whatever the
// desktop happened to look like when you last closed it. This module keeps a
// *set* of named layouts plus a pointer to the last-active one, so a research
// session ("bench", "triage", "writing") can be parked and returned to
// deliberately instead of by accident.
//
// What a session stores is exactly a `LayoutState`: the pane tree, pane types
// and params, every one of its workspaces (however many there are — the
// count is no longer fixed, see `MIN_WORKSPACES`/`MAX_WORKSPACES` in
// layout.ts), and focus. It deliberately does NOT store
// anything live — no PTY handles, no scrollback, no running agent. Restoring a
// session re-opens the same *shape* with fresh shells; process resurrection is
// a different (much harder) feature and pretending to do it here would be a
// lie the title row couldn't back up.
//
// Like `layout.ts` this is pure and framework-free. The two functions that do
// touch storage take a `SessionStorage` interface rather than reaching for the
// global `localStorage`, so the whole module is unit-testable with a plain
// object and no DOM. Layout validation is *not* re-implemented here: stored
// layouts are checked with `layout.ts`'s own `isValidLayoutState`, so there is
// exactly one definition of "is this a layout" in the codebase.

import {
  defaultLayout,
  deserialize,
  isValidLayoutState,
  rsiLayout,
  type LayoutState,
  type Node,
} from "./layout";
import { isRsiParams, type RsiParams } from "./rsi";

export const SESSIONS_KEY = "turing.sessions.v1";
// Marks that the one-time reseed below has already run for this profile. Its
// presence is the entire record — the reseed must never repeat, or every
// launch would bulldoze whatever the user arranged since the last one.
export const RESEED_KEY = "turing.sessions.reseed.v1";
// The pre-sessions single-layout key. Read once, on the first launch under the
// new scheme, and then left alone — never written and never deleted, so an
// older build (or a rolled-back one) still finds the layout it expects.
export const LEGACY_LAYOUT_KEY = "turing.layout.v2";
export const DEFAULT_SESSION_NAME = "default";

// "normal" (the default, and the only kind that existed before RSI
// workstations) is a saved pane layout that reopens the same shape with
// fresh shells. "rsi" is an experiment: it seeds a loop-terminal layout (see
// `rsiLayout()` in layout.ts) whose loop is pre-typed, never auto-started.
export type SessionKind = "normal" | "rsi";

export interface Session {
  id: string;
  name: string;
  layout: LayoutState;
  createdAt: number;
  updatedAt: number;
  // Absent means "normal" — normal sessions never carry this key, so their
  // serialized shape is byte-for-byte what it was before RSI workstations
  // existed. Only set it to "rsi"; never write "normal" explicitly.
  kind?: SessionKind;
}

// Keyed by session id, not by name: names are user-facing, editable, and not
// required to be unique, so they make a terrible primary key. `activeId` is
// the "last used" pointer the startup picker defaults to.
export interface SessionStore {
  sessions: Record<string, Session>;
  activeId: string | null;
}

// The slice of `Storage` this module needs. Narrower than the DOM interface on
// purpose — tests pass a `Map`-backed stub, and nothing here wants `length`,
// `key()` or `clear()`.
export interface SessionStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

let idCounter = 0;

// Session ids only have to be unique within one browser profile, so a
// timestamp plus a module-local counter is enough: the counter disambiguates
// sessions created inside the same millisecond, and the timestamp keeps ids
// from colliding across restarts (which reset the counter).
export function nextSessionId(now: number = Date.now()): string {
  idCounter += 1;
  return `s-${now.toString(36)}-${idCounter}`;
}

export function emptyStore(): SessionStore {
  return { sessions: {}, activeId: null };
}

function cleanName(name: string): string {
  return name.trim() || DEFAULT_SESSION_NAME;
}

// Ordering for every list the user sees (picker, launcher): most recently
// touched first, so "the one I was just in" is always the top row and Enter on
// a freshly opened picker is the least surprising thing possible. Name is the
// tie-break so the order is total and stable rather than dependent on object
// key order.
export function listSessions(store: SessionStore): Session[] {
  return Object.values(store.sessions).sort(
    (a, b) => b.updatedAt - a.updatedAt || a.name.localeCompare(b.name),
  );
}

// The session whose layout should be on screen. Falls back to the most
// recently updated one when `activeId` is missing or dangling, so a store that
// lost its pointer (hand-edited, half-written, migrated from elsewhere) still
// opens on something sensible instead of a blank desktop.
export function activeSession(store: SessionStore): Session | null {
  if (store.activeId) {
    const found = store.sessions[store.activeId];
    if (found) return found;
  }
  return listSessions(store)[0] ?? null;
}

export function createSession(
  store: SessionStore,
  name: string,
  layout: LayoutState,
  now: number = Date.now(),
  id: string = nextSessionId(now),
  kind?: SessionKind,
): SessionStore {
  const session: Session = {
    id,
    name: cleanName(name),
    layout,
    createdAt: now,
    updatedAt: now,
    ...(kind === "rsi" ? { kind: "rsi" as const } : {}),
  };
  return { sessions: { ...store.sessions, [id]: session }, activeId: id };
}

// Wraps `createSession` with `kind: "rsi"` so DesktopShell's "new rsi
// experiment" flow doesn't have to pass positional `now`/`id` just to reach
// the sixth parameter.
export function createRsiSession(store: SessionStore, name: string, layout: LayoutState): SessionStore {
  return createSession(store, name, layout, undefined, undefined, "rsi");
}

export function renameSession(
  store: SessionStore,
  id: string,
  name: string,
  now: number = Date.now(),
): SessionStore {
  const session = store.sessions[id];
  if (!session) return store;
  return {
    ...store,
    sessions: { ...store.sessions, [id]: { ...session, name: cleanName(name), updatedAt: now } },
  };
}

// Deleting the active session hands `activeId` to the next most recently used
// one; deleting the last session leaves the store empty with a null pointer,
// which `saveLayoutInto` will later refill with a fresh "default". An unknown
// id is a no-op.
export function deleteSession(store: SessionStore, id: string): SessionStore {
  if (!store.sessions[id]) return store;
  const sessions = { ...store.sessions };
  delete sessions[id];
  const next: SessionStore = { sessions, activeId: store.activeId };
  if (store.activeId === id) {
    next.activeId = listSessions(next)[0]?.id ?? null;
  }
  return next;
}

export function switchSession(store: SessionStore, id: string): SessionStore {
  if (!store.sessions[id] || store.activeId === id) return store;
  return { ...store, activeId: id };
}

// The autosave path: fold the live layout into whichever session is currently
// active. This is what keeps ⌘Return-ing a new terminal from writing into a
// global blob — every edit lands in the session the user is actually in.
// With no active session (first run, or the user just deleted their last one)
// it mints "default", so autosave is always a complete persistence story and
// callers never have to special-case an empty store.
export function saveLayoutInto(
  store: SessionStore,
  layout: LayoutState,
  now: number = Date.now(),
): SessionStore {
  const current = activeSession(store);
  if (!current) return createSession(store, DEFAULT_SESSION_NAME, layout, now);
  return {
    sessions: {
      ...store.sessions,
      [current.id]: { ...current, layout, updatedAt: now },
    },
    activeId: current.id,
  };
}

export function serializeSessions(store: SessionStore): string {
  return JSON.stringify(store);
}

function isSession(x: unknown): x is Session {
  if (!x || typeof x !== "object") return false;
  const o = x as Record<string, unknown>;
  return (
    typeof o.id === "string" &&
    typeof o.name === "string" &&
    typeof o.createdAt === "number" &&
    typeof o.updatedAt === "number" &&
    isValidLayoutState(o.layout) &&
    (o.kind === undefined || o.kind === "normal" || o.kind === "rsi")
  );
}

// Drops individually-corrupt sessions rather than failing the whole store: one
// bad layout (a half-written entry, a blob from a future schema) should cost
// you that session, not every session you have.
export function deserializeSessions(s: string): SessionStore | null {
  try {
    const parsed: unknown = JSON.parse(s);
    if (!parsed || typeof parsed !== "object") return null;
    const o = parsed as Record<string, unknown>;
    if (!o.sessions || typeof o.sessions !== "object" || Array.isArray(o.sessions)) return null;
    if (o.activeId !== null && typeof o.activeId !== "string") return null;

    const sessions: Record<string, Session> = {};
    for (const [id, value] of Object.entries(o.sessions as Record<string, unknown>)) {
      if (isSession(value) && value.id === id) sessions[id] = value;
    }
    const activeId = typeof o.activeId === "string" && sessions[o.activeId] ? o.activeId : null;
    return { sessions, activeId };
  } catch {
    return null;
  }
}

// Pull the `rsi` params back out of a stored layout. An RSI workstation's
// identity — which sandbox it drives, which problem it was given — lives
// nowhere but the loop terminal's leaf params, so reseeding one without
// recovering these first would silently demote it to an ordinary desktop
// with the same name.
function findRsiParams(root: Node | null): RsiParams | null {
  if (!root) return null;
  if (root.kind === "split") return findRsiParams(root.a) ?? findRsiParams(root.b);
  const rsi = root.params?.rsi;
  if (rsi && typeof rsi === "object") {
    if (isRsiParams(rsi)) return rsi;
  }
  return null;
}

// Same recovery, but over every workspace in the layout rather than just one.
// The loop terminal can live on any workspace — the send-to-workspace chord
// (⌘⇧1..5) moves panes freely — so a search that only looked at workspace 0
// would return null for an rsi workstation whose terminal had been moved,
// and the reseed would silently rebuild it as `defaultLayout()`, losing the
// slug/problem for good (the reseed only ever runs once). Workspaces are
// searched in order and the first match wins; two loop terminals with rsi
// params in the same workstation would be unusual (nothing creates more than
// one), but if it ever happened, keeping the lowest-indexed workspace's
// params is at least deterministic.
function findRsiParamsInLayout(layout: LayoutState): RsiParams | null {
  for (const ws of layout.workspaces) {
    const found = findRsiParams(ws.root ?? null);
    if (found) return found;
  }
  return null;
}

// Replace every stored layout with the current preset, keeping the workstation
// itself — id, name, kind, createdAt — intact.
//
// Owen asked for this explicitly after the seeded set went from five
// workspaces to three: a saved workstation reopens the *shape* it was saved
// with, so changing `defaultLayout()` alone left every existing workstation
// still on the old five-workspace spread, and the new default was only ever
// visible on a workstation created after the change. This is the override.
//
// It is deliberately a reseed, not a reshape: there is no honest way to map an
// arbitrary five-workspace arrangement onto the three-workspace preset, and a
// half-migrated tree would be worse than a clean one. Pane *arrangement* is
// what's discarded; nothing live is, since layouts never stored processes or
// scrollback in the first place. RSI workstations keep their loop terminal by
// rebuilding through `rsiLayout()` with the params recovered above.
export function reseedLayouts(store: SessionStore, now: number = Date.now()): SessionStore {
  const sessions: Record<string, Session> = {};
  for (const [id, session] of Object.entries(store.sessions)) {
    const rsi = session.kind === "rsi" ? findRsiParamsInLayout(session.layout) : null;
    const config = rsi?.engine || rsi?.verifier
      ? { engine: rsi.engine ?? ("claude" as const), verifier: rsi.verifier ?? "" }
      : undefined;
    sessions[id] = {
      ...session,
      layout: rsi ? rsiLayout(rsi.slug, rsi.problem, config) : defaultLayout(),
      updatedAt: now,
    };
  }
  return { sessions, activeId: store.activeId };
}

// Read the store, migrating on the way if this is the first launch under the
// sessions scheme. Precedence:
//
//   1. a valid sessions blob            → use it
//   2. a valid legacy single layout     → adopt it as one session, "default"
//   3. anything else (absent, garbage)  → empty store
//
// Case 2 is the whole point of the migration: an existing user's desktop must
// come back exactly as they left it, just now with a name on it. Case 3 covers
// both the brand-new install and the corrupt-storage case — neither throws,
// and the caller falls back to `defaultLayout()`.
export function loadSessions(storage: SessionStorage, now: number = Date.now()): SessionStore {
  let raw: string | null = null;
  try {
    raw = storage.getItem(SESSIONS_KEY);
  } catch {
    return emptyStore();
  }

  // The reseed is checked before anything is returned, and the marker write
  // is attempted whether or not there was a store to reseed — a profile that
  // reaches this line has, by definition, now been through it, and skipping
  // the write on an empty store would leave the reseed armed to fire later
  // against workstations the user creates *after* the change.
  //
  // This is a destructive one-time migration, so it may run only when there
  // is a durable record that it did. A `setItem` call that doesn't throw is
  // NOT proof the write landed: on a near-full origin, writing an *existing*
  // key can succeed while allocating a brand-new key throws
  // `QuotaExceededError`, and Safari private mode is documented to accept
  // some writes while silently dropping others. So the marker is read back
  // rather than trusted, and `shouldReseed` only goes true when that
  // readback confirms the write actually stuck. Any failure to durably
  // record the marker — a throw from either call, or a write that silently
  // didn't persist — leaves `shouldReseed` false, which skips the reseed for
  // this launch. A skipped migration is recoverable (it's retried on the
  // next launch, once/if storage frees up); a reseed that ran without a
  // durable marker would run again on *every* launch, since the absent
  // marker gives it nothing to remember it by — destroying whatever the user
  // rearranges in between, forever, not just once.
  let shouldReseed = false;
  try {
    const alreadyMarked = storage.getItem(RESEED_KEY) !== null;
    if (!alreadyMarked) {
      storage.setItem(RESEED_KEY, String(now));
      shouldReseed = storage.getItem(RESEED_KEY) !== null;
    }
  } catch {
    // Unreadable/unwritable storage is handled below by the same paths that
    // already treat it as "no saved state"; never let it throw out of here.
    // `shouldReseed` stays false, which is the safe direction to fail in.
    shouldReseed = false;
  }

  // Committing the reseed writes it straight back to storage rather than
  // waiting for the shell's autosave. Autosave is gated on `!home`, and the
  // app boots on Home: a user who launches, reads the workstation list and
  // quits without opening one would otherwise burn the marker while the store
  // on disk stayed on the old preset — the migration recorded as done, having
  // changed nothing, with no second chance because the marker is checked
  // before anything else. Writing here closes that window; the marker and the
  // layouts it describes land in the same call. A failed write is swallowed
  // by `saveSessions` as everywhere else, which leaves the old layouts on
  // disk under a set marker — the reseed is simply skipped, never repeated.
  function commit(store: SessionStore): SessionStore {
    saveSessions(storage, store);
    return store;
  }

  if (raw) {
    const parsed = deserializeSessions(raw);
    if (parsed) return shouldReseed ? commit(reseedLayouts(parsed, now)) : parsed;
  }

  let legacyRaw: string | null = null;
  try {
    legacyRaw = storage.getItem(LEGACY_LAYOUT_KEY);
  } catch {
    return emptyStore();
  }
  if (legacyRaw) {
    // `deserialize` is layout.ts's own parse-and-validate; a legacy blob is
    // literally what it was written to read.
    const legacy = deserialize(legacyRaw);
    if (legacy) {
      // Reseeded on the same terms as the sessions blob above: a layout this
      // old is guaranteed to predate the three-workspace preset, so adopting
      // it verbatim would reintroduce exactly what the reseed exists to clear.
      const adopted = createSession(emptyStore(), DEFAULT_SESSION_NAME, legacy, now);
      return shouldReseed ? commit(reseedLayouts(adopted, now)) : adopted;
    }
  }

  return emptyStore();
}

// Best-effort by design, like `theme.ts`: a storage write that fails (quota,
// Safari private mode) costs the user their session list on next launch, which
// is bad, but throwing out of a React effect and blanking the desktop is
// worse.
export function saveSessions(storage: SessionStorage, store: SessionStore): void {
  try {
    storage.setItem(SESSIONS_KEY, serializeSessions(store));
  } catch {
    /* ignore */
  }
}
