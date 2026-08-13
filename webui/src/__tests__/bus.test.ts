// Event-bus tests (issue #382 follow-up).
//
// The bug being pinned: `@tauri-apps/api` corrupts its own listener registry
// when an unlisten runs during dispatch —
//   TypeError: undefined is not an object (evaluating 'listeners[eventId].handlerId')
// React.StrictMode's dev double-mount (mount → cleanup → remount) makes every
// pane do exactly that on every launch, after which delivery for that event is
// broken and panes that re-subscribe receive nothing.
//
// The bus fixes it by attaching ONE Tauri listener per event name for the app's
// lifetime and never unlistening; subscribe/unsubscribe are local-only. These
// tests hold that contract, mocking the `listen` seam.

import { beforeEach, describe, expect, it, vi } from "vitest";

// Handlers registered against the mocked Tauri `listen`, keyed by event name.
const attached = new Map<string, (e: { payload: unknown }) => void>();
const listenCalls: string[] = [];
const unlistenCalls: string[] = [];
let listenResolvers: Array<() => void> = [];
/** When true, `listen` stays pending until `flushListens()` is called. */
let deferListen = false;

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/event", () => ({
  listen: (event: string, handler: (e: { payload: unknown }) => void) => {
    listenCalls.push(event);
    attached.set(event, handler);
    const unlisten = () => unlistenCalls.push(event);
    if (!deferListen) return Promise.resolve(unlisten);
    return new Promise<typeof unlisten>((resolve) => {
      listenResolvers.push(() => resolve(unlisten));
    });
  },
}));

function flushListens() {
  for (const r of listenResolvers) r();
  listenResolvers = [];
}

/** Simulate the Rust side emitting an event. */
function emit(event: string, payload: unknown) {
  const handler = attached.get(event);
  if (!handler) throw new Error(`nothing listening for ${event}`);
  handler({ payload });
}

let subscribe: typeof import("../desktop/tauri").subscribe;
let reset: typeof import("../desktop/tauri").__resetEventBusForTests;

beforeEach(async () => {
  attached.clear();
  listenCalls.length = 0;
  unlistenCalls.length = 0;
  listenResolvers = [];
  deferListen = false;
  const mod = await import("../desktop/tauri");
  subscribe = mod.subscribe;
  reset = mod.__resetEventBusForTests;
  reset();
});

describe("event bus", () => {
  it("performs exactly one underlying listen per event name", () => {
    for (let i = 0; i < 5; i++) subscribe("fs-change", () => {});
    subscribe("pty-output", () => {});
    expect(listenCalls).toEqual(["fs-change", "pty-output"]);
  });

  it("fans one event out to every subscriber", () => {
    const seen: string[] = [];
    subscribe<string>("fs-change", (p) => seen.push(`a:${p}`));
    subscribe<string>("fs-change", (p) => seen.push(`b:${p}`));
    emit("fs-change", "x");
    expect(seen).toEqual(["a:x", "b:x"]);
  });

  it("keeps delivering to siblings when one unsubscribes mid-dispatch", () => {
    // The exact shape that corrupts the raw Tauri registry.
    const seen: string[] = [];
    const first = subscribe<string>("pty-output", (p) => {
      seen.push(`first:${p}`);
      first.unsubscribe();
    });
    subscribe<string>("pty-output", (p) => seen.push(`second:${p}`));
    subscribe<string>("pty-output", (p) => seen.push(`third:${p}`));

    emit("pty-output", "1");
    expect(seen).toEqual(["first:1", "second:1", "third:1"]);

    // Only the unsubscriber drops out; the rest keep working.
    emit("pty-output", "2");
    expect(seen).toEqual(["first:1", "second:1", "third:1", "second:2", "third:2"]);
  });

  it("keeps delivering when a subscriber throws", () => {
    const seen: string[] = [];
    subscribe("fs-change", () => {
      throw new Error("render bug");
    });
    subscribe<string>("fs-change", (p) => seen.push(p));
    expect(() => emit("fs-change", "ok")).not.toThrow();
    expect(seen).toEqual(["ok"]);
  });

  it("never calls the underlying unlisten, even when all subscribers leave", () => {
    const a = subscribe("fs-change", () => {});
    const b = subscribe("fs-change", () => {});
    a.unsubscribe();
    b.unsubscribe();
    expect(unlistenCalls).toEqual([]);

    // The listener is still attached, so a later subscriber still receives.
    const seen: string[] = [];
    subscribe<string>("fs-change", (p) => seen.push(p));
    emit("fs-change", "after");
    expect(seen).toEqual(["after"]);
    // Still only ever one listen for this event.
    expect(listenCalls).toEqual(["fs-change"]);
  });

  it("survives a full StrictMode mount/cleanup/remount cycle", () => {
    const seen: string[] = [];
    // First mount, then its cleanup — the sequence that used to poison things.
    const firstMount = subscribe<string>("pty-output", (p) => seen.push(`m1:${p}`));
    firstMount.unsubscribe();
    // Remount.
    subscribe<string>("pty-output", (p) => seen.push(`m2:${p}`));

    emit("pty-output", "prompt");
    expect(seen).toEqual(["m2:prompt"]);
    expect(listenCalls).toEqual(["pty-output"]);
    expect(unlistenCalls).toEqual([]);
  });

  it("dedupes concurrent first-subscriptions into one listen", async () => {
    deferListen = true;
    const a = subscribe("pty-output", () => {});
    const b = subscribe("pty-output", () => {});
    expect(listenCalls).toEqual(["pty-output"]);

    // Both share the single in-flight attach.
    let ready = 0;
    void a.ready.then(() => ready++);
    void b.ready.then(() => ready++);
    flushListens();
    await Promise.all([a.ready, b.ready]);
    expect(ready).toBe(2);
  });

  it("resolves ready only once the underlying listen has attached", async () => {
    deferListen = true;
    const sub = subscribe("pty-output", () => {});
    let attachedYet = false;
    void sub.ready.then(() => (attachedYet = true));
    await Promise.resolve();
    expect(attachedYet).toBe(false);

    flushListens();
    await sub.ready;
    expect(attachedYet).toBe(true);
  });

  it("gives each subscription its own identity for the same callback", () => {
    const seen: string[] = [];
    const cb = (p: string) => seen.push(p);
    const a = subscribe<string>("fs-change", cb);
    subscribe<string>("fs-change", cb);
    a.unsubscribe();
    emit("fs-change", "one");
    // The second registration of the same function reference survives.
    expect(seen).toEqual(["one"]);
  });

  it("is idempotent on repeated unsubscribe", () => {
    const seen: string[] = [];
    const a = subscribe<string>("fs-change", (p) => seen.push(`a:${p}`));
    subscribe<string>("fs-change", (p) => seen.push(`b:${p}`));
    a.unsubscribe();
    a.unsubscribe();
    emit("fs-change", "x");
    expect(seen).toEqual(["b:x"]);
  });
});
