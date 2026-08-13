// Saved-session tests — the store is pure and its two storage functions take
// a `SessionStorage` interface, so everything here runs against a Map-backed
// stub with no DOM involved.

import { describe, expect, it } from "vitest";
import { defaultLayout, emptyLayout, openPane, serialize, type LayoutState } from "../desktop/layout";
import {
  activeSession,
  createSession,
  deleteSession,
  deserializeSessions,
  emptyStore,
  LEGACY_LAYOUT_KEY,
  listSessions,
  loadSessions,
  renameSession,
  saveLayoutInto,
  saveSessions,
  serializeSessions,
  SESSIONS_KEY,
  switchSession,
  type SessionStorage,
} from "../desktop/sessions";



function memStorage(seed: Record<string, string> = {}): SessionStorage & { map: Map<string, string> } {
  const map = new Map<string, string>(Object.entries(seed));
  return {
    map,
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => {
      map.set(k, v);
    },
  };
}

function layoutWith(...panes: string[]): LayoutState {
  let state = emptyLayout();
  for (const id of panes) state = openPane(state, "term", undefined, id);
  return state;
}

describe("save/load round-trip", () => {
  it("restores sessions, names and the active pointer through storage", () => {
    const storage = memStorage();
    const store = createSession(emptyStore(), "bench", layoutWith("a", "b"), 1000, "s1");
    saveSessions(storage, store);

    const loaded = loadSessions(storage);
    expect(Object.keys(loaded.sessions)).toEqual(["s1"]);
    expect(loaded.activeId).toBe("s1");
    const restored = activeSession(loaded);
    expect(restored?.name).toBe("bench");
    expect(serialize(restored!.layout)).toBe(serialize(store.sessions.s1.layout));
    expect(restored?.createdAt).toBe(1000);
  });

  it("round-trips several sessions and keeps them independent", () => {
    let store = createSession(emptyStore(), "bench", layoutWith("a"), 1000, "s1");
    store = createSession(store, "triage", layoutWith("b", "c"), 2000, "s2");
    const loaded = deserializeSessions(serializeSessions(store))!;

    expect(listSessions(loaded).map((s) => s.name)).toEqual(["triage", "bench"]);
    expect(loaded.activeId).toBe("s2");
  });

  it("orders sessions most-recently-updated first", () => {
    let store = createSession(emptyStore(), "old", layoutWith("a"), 1000, "s1");
    store = createSession(store, "new", layoutWith("b"), 2000, "s2");
    store = renameSession(store, "s1", "old", 3000);
    expect(listSessions(store).map((s) => s.id)).toEqual(["s1", "s2"]);
  });
});

describe("migration from the legacy single-layout blob", () => {
  it("adopts localStorage['turing.layout.v2'] as a session named default", () => {
    const legacy = layoutWith("a", "b");
    const storage = memStorage({ [LEGACY_LAYOUT_KEY]: serialize(legacy) });

    const loaded = loadSessions(storage, 42);
    const only = activeSession(loaded);
    expect(listSessions(loaded)).toHaveLength(1);
    expect(only?.name).toBe("default");
    expect(serialize(only!.layout)).toBe(serialize(legacy));
    expect(only?.createdAt).toBe(42);
    expect(loaded.activeId).toBe(only?.id);
  });

  it("leaves the legacy key in place so an older build still finds it", () => {
    const legacy = serialize(layoutWith("a"));
    const storage = memStorage({ [LEGACY_LAYOUT_KEY]: legacy });
    loadSessions(storage);
    expect(storage.map.get(LEGACY_LAYOUT_KEY)).toBe(legacy);
  });

  it("prefers a real sessions blob over the legacy one", () => {
    const store = createSession(emptyStore(), "bench", layoutWith("a"), 1000, "s1");
    const storage = memStorage({
      [SESSIONS_KEY]: serializeSessions(store),
      [LEGACY_LAYOUT_KEY]: serialize(defaultLayout()),
    });
    expect(listSessions(loadSessions(storage)).map((s) => s.name)).toEqual(["bench"]);
  });

  it("does not re-migrate once an empty store has been written", () => {
    const storage = memStorage({ [LEGACY_LAYOUT_KEY]: serialize(layoutWith("a")) });
    saveSessions(storage, emptyStore());
    expect(listSessions(loadSessions(storage))).toEqual([]);
  });

  it("gives a user with nothing saved an empty store (caller uses defaultLayout)", () => {
    expect(loadSessions(memStorage())).toEqual({ sessions: {}, activeId: null });
  });
});

describe("switching sessions", () => {
  it("moves the active pointer and leaves both layouts untouched", () => {
    let store = createSession(emptyStore(), "bench", layoutWith("a"), 1000, "s1");
    store = createSession(store, "triage", layoutWith("b"), 2000, "s2");

    const switched = switchSession(store, "s1");
    expect(switched.activeId).toBe("s1");
    expect(activeSession(switched)?.name).toBe("bench");
    expect(switched.sessions.s2.layout).toBe(store.sessions.s2.layout);
  });

  it("ignores an unknown id", () => {
    const store = createSession(emptyStore(), "bench", layoutWith("a"), 1000, "s1");
    expect(switchSession(store, "nope")).toBe(store);
  });

  it("autosaves the live layout into the active session only", () => {
    let store = createSession(emptyStore(), "bench", layoutWith("a"), 1000, "s1");
    store = createSession(store, "triage", layoutWith("b"), 2000, "s2");

    const edited = openPane(store.sessions.s2.layout, "metrics", undefined, "c");
    const saved = saveLayoutInto(store, edited, 3000);

    expect(saved.sessions.s2.layout).toBe(edited);
    expect(saved.sessions.s2.updatedAt).toBe(3000);
    expect(saved.sessions.s1.layout).toBe(store.sessions.s1.layout);
    expect(saved.sessions.s1.updatedAt).toBe(1000);
  });

  it("mints a default session when autosaving with nothing active", () => {
    const saved = saveLayoutInto(emptyStore(), layoutWith("a"), 500);
    const only = activeSession(saved);
    expect(only?.name).toBe("default");
    expect(only?.createdAt).toBe(500);
  });
});

describe("deleting", () => {
  it("hands the active pointer to the next most recent session", () => {
    let store = createSession(emptyStore(), "bench", layoutWith("a"), 1000, "s1");
    store = createSession(store, "triage", layoutWith("b"), 2000, "s2");
    store = createSession(store, "writing", layoutWith("c"), 3000, "s3");

    const after = deleteSession(store, "s3");
    expect(Object.keys(after.sessions).sort()).toEqual(["s1", "s2"]);
    expect(after.activeId).toBe("s2");
  });

  it("leaves an empty store with a null pointer when the last one goes", () => {
    const store = createSession(emptyStore(), "bench", layoutWith("a"), 1000, "s1");
    const after = deleteSession(store, "s1");
    expect(after).toEqual({ sessions: {}, activeId: null });
    expect(activeSession(after)).toBeNull();
  });

  it("keeps the active pointer when deleting a different session", () => {
    let store = createSession(emptyStore(), "bench", layoutWith("a"), 1000, "s1");
    store = createSession(store, "triage", layoutWith("b"), 2000, "s2");
    const after = deleteSession(store, "s1");
    expect(after.activeId).toBe("s2");
  });

  it("ignores an unknown id", () => {
    const store = createSession(emptyStore(), "bench", layoutWith("a"), 1000, "s1");
    expect(deleteSession(store, "nope")).toBe(store);
  });
});

describe("renaming", () => {
  it("renames in place and bumps updatedAt", () => {
    const store = createSession(emptyStore(), "bench", layoutWith("a"), 1000, "s1");
    const after = renameSession(store, "s1", "  benchmarks  ", 2000);
    expect(after.sessions.s1.name).toBe("benchmarks");
    expect(after.sessions.s1.updatedAt).toBe(2000);
    expect(after.sessions.s1.createdAt).toBe(1000);
  });

  it("falls back to 'default' rather than accepting an empty name", () => {
    const store = createSession(emptyStore(), "   ", layoutWith("a"), 1000, "s1");
    expect(store.sessions.s1.name).toBe("default");
  });
});

describe("corrupt storage", () => {
  it("falls back to an empty store on unparseable json", () => {
    expect(loadSessions(memStorage({ [SESSIONS_KEY]: "{not json" }))).toEqual({
      sessions: {},
      activeId: null,
    });
  });

  it("falls back to an empty store on a wrong-shaped blob", () => {
    for (const raw of ["null", "42", '"a string"', "[1,2,3]", '{"sessions":[]}']) {
      expect(loadSessions(memStorage({ [SESSIONS_KEY]: raw }))).toEqual({
        sessions: {},
        activeId: null,
      });
    }
  });

  it("falls back to the legacy blob when the sessions blob is garbage", () => {
    const legacy = layoutWith("a");
    const loaded = loadSessions(
      memStorage({ [SESSIONS_KEY]: "}}}", [LEGACY_LAYOUT_KEY]: serialize(legacy) }),
      7,
    );
    expect(activeSession(loaded)?.name).toBe("default");
  });

  it("ignores a legacy blob that is itself garbage", () => {
    expect(loadSessions(memStorage({ [LEGACY_LAYOUT_KEY]: '{"workspaces":"nope"}' }))).toEqual({
      sessions: {},
      activeId: null,
    });
  });

  it("drops only the sessions whose layout is invalid", () => {
    const good = createSession(emptyStore(), "bench", layoutWith("a"), 1000, "s1");
    const raw = JSON.parse(serializeSessions(good)) as Record<string, unknown>;
    (raw.sessions as Record<string, unknown>).s2 = {
      id: "s2",
      name: "broken",
      layout: { workspaces: "nope", active: 0 },
      createdAt: 1,
      updatedAt: 1,
    };
    const loaded = loadSessions(memStorage({ [SESSIONS_KEY]: JSON.stringify(raw) }));
    expect(listSessions(loaded).map((s) => s.name)).toEqual(["bench"]);
  });

  it("drops a dangling activeId instead of pointing at nothing", () => {
    const store = createSession(emptyStore(), "bench", layoutWith("a"), 1000, "s1");
    const raw = { ...JSON.parse(serializeSessions(store)), activeId: "gone" };
    const loaded = loadSessions(memStorage({ [SESSIONS_KEY]: JSON.stringify(raw) }));
    expect(loaded.activeId).toBeNull();
    // Still opens on something: the most recently updated survivor.
    expect(activeSession(loaded)?.name).toBe("bench");
  });

  it("survives storage that throws on read", () => {
    const throwing: SessionStorage = {
      getItem() {
        throw new Error("SecurityError");
      },
      setItem() {},
    };
    expect(loadSessions(throwing)).toEqual({ sessions: {}, activeId: null });
  });

  it("swallows a failing write rather than blanking the desktop", () => {
    const throwing: SessionStorage = {
      getItem: () => null,
      setItem() {
        throw new Error("QuotaExceededError");
      },
    };
    expect(() => saveSessions(throwing, emptyStore())).not.toThrow();
  });
});

describe("session kind (RSI workstations)", () => {
  it("sets kind on an rsi session and omits it entirely for a normal one", () => {
    const rsi = createSession(emptyStore(), "experiment", layoutWith("a"), 1000, "s1", "rsi");
    expect(rsi.sessions.s1.kind).toBe("rsi");

    const normal = createSession(emptyStore(), "bench", layoutWith("a"), 1000, "s2");
    expect("kind" in normal.sessions.s2).toBe(false);
  });

  it("round-trips an rsi session's kind through serialize/deserialize", () => {
    const store = createSession(emptyStore(), "experiment", layoutWith("a"), 1000, "s1", "rsi");
    const loaded = deserializeSessions(serializeSessions(store))!;
    expect(loaded.sessions.s1.kind).toBe("rsi");
  });

  it("drops a session with an invalid kind while keeping valid siblings", () => {
    const good = createSession(emptyStore(), "bench", layoutWith("a"), 1000, "s1", "rsi");
    const raw = JSON.parse(serializeSessions(good)) as Record<string, unknown>;
    (raw.sessions as Record<string, unknown>).s2 = {
      id: "s2",
      name: "broken",
      layout: layoutWith("b"),
      createdAt: 1,
      updatedAt: 1,
      kind: "bogus",
    };
    const loaded = loadSessions(memStorage({ [SESSIONS_KEY]: JSON.stringify(raw) }));
    expect(Object.keys(loaded.sessions)).toEqual(["s1"]);
    expect(loaded.sessions.s1.kind).toBe("rsi");
  });

  it("preserves kind when saveLayoutInto folds a new layout into an rsi session", () => {
    const store = createSession(emptyStore(), "experiment", layoutWith("a"), 1000, "s1", "rsi");
    const saved = saveLayoutInto(store, layoutWith("a", "b"), 2000);
    expect(saved.sessions.s1.kind).toBe("rsi");
  });
});
