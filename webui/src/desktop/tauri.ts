// Single choke point for `@tauri-apps/api` imports. No other module in this
// codebase imports `@tauri-apps/api` directly — everything routes through
// `isTauri()` / `inv()` / `subscribe()` here, so the rest of the tree stays
// safe to import in jsdom/browser without a Tauri runtime present.

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

export function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export function inv<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  return invoke<T>(cmd, args);
}

// ---------------------------------------------------------------------------
// Event bus
// ---------------------------------------------------------------------------
//
// Why this exists instead of each caller doing its own listen/unlisten:
//
// `@tauri-apps/api` keeps its listeners in a per-event object keyed by id, and
// running an unlisten *while that event is being dispatched* corrupts the
// walk — the launch log's `TypeError: undefined is not an object (evaluating
// 'listeners[eventId].handlerId')`. Once that fires, delivery is broken for
// the remaining listeners of that event, so a pane that re-subscribes
// afterwards receives nothing: blank terminals and dead typing.
//
// React.StrictMode makes that sequence happen on every single pane, every
// launch. Its dev double-mount runs mount → cleanup → remount, so each pane
// registers a listener and tears it down again with events already in flight.
// StrictMode is not the bug and stays on; it just reveals that per-subscriber
// unlisten is unsafe.
//
// So the Tauri listener becomes app-lifetime infrastructure. The first
// subscriber for an event name performs exactly ONE `listen()`, and the
// unlisten it returns is deliberately never called. Subscribing and
// unsubscribing after that are purely local Set operations, which no amount of
// mounting and unmounting can corrupt. A permanently-attached listener per
// event name is a bounded, trivial cost — there are a handful of event names
// and they are all live for the app's lifetime anyway.
//
// Ordering note: `subscribe()` is synchronous, so the channel (and its single
// in-flight `listen()` promise) is created and registered before any await can
// interleave. Concurrent "first" subscribers therefore share one `listen()`
// automatically rather than racing to create two.
//
// Delivery gap: Tauri drops events emitted before `listen()` resolves — it
// cannot queue what it is not yet listening for. `subscribe()` returns
// immediately while that attach is still in flight, so a caller that is about
// to *cause* emissions must `await sub.ready` first. TermPane depends on this:
// it awaits both subscriptions before asking Rust to spawn the pty.

interface Subscriber {
  cb: (payload: never) => void;
}

interface Channel {
  subscribers: Set<Subscriber>;
  ready: Promise<void>;
}

const channels = new Map<string, Channel>();

export interface Subscription {
  /** Removes only this callback. The underlying Tauri listener stays attached. */
  unsubscribe: () => void;
  /** Resolves once the underlying `listen()` has attached. */
  ready: Promise<void>;
}

export function subscribe<T>(event: string, cb: (payload: T) => void): Subscription {
  let channel = channels.get(event);
  if (!channel) {
    const subscribers = new Set<Subscriber>();
    const ready = listen(event, (e: { payload: unknown }) => {
      // Iterate a snapshot: a callback is allowed to unsubscribe (or
      // subscribe) during dispatch, and mutating the live Set mid-iteration is
      // what makes that unsafe. Copying also means an unsubscribe taking
      // effect mid-dispatch cannot skip a sibling subscriber.
      for (const sub of [...subscribers]) {
        // One throwing subscriber must not deprive the rest of the event.
        try {
          (sub.cb as (p: unknown) => void)(e.payload);
        } catch {
          // Swallowed deliberately; a pane's render bug is not a bus fault.
        }
      }
    }).then(() => undefined);
    channel = { subscribers, ready };
    channels.set(event, channel);
  }

  // A fresh wrapper per call, so identity is per-subscription: two callers
  // passing the same function reference get two independent subscriptions.
  const sub: Subscriber = { cb: cb as (payload: never) => void };
  channel.subscribers.add(sub);

  const owner = channel;
  let released = false;
  return {
    unsubscribe: () => {
      if (released) return;
      released = true;
      owner.subscribers.delete(sub);
    },
    ready: channel.ready,
  };
}

/** Test seam: drops all channels so a fresh module state can be asserted. */
export function __resetEventBusForTests(): void {
  channels.clear();
}
