// Ordered delivery of `pty-output` across the window where the pane is
// listening but does not yet know which pty id is its own.
//
// The Rust reader thread starts emitting the instant `pty_spawn` creates it,
// and a Tauri event with no registered listener is *dropped*, not queued. So
// subscribing after `await pty_spawn(...)` loses everything the shell printed
// before the IPC response made it back — which is the entire oh-my-posh
// prompt burst. (This stayed invisible while TERM was unset, because a shell
// with no terminfo prints almost nothing at startup. Fixing TERM filled the
// gap with real output and turned the pane blank.)
//
// Subscribing *before* spawning closes the drop window but opens a second
// problem: events for every pane's pty now arrive with no way to tell which
// are ours. This buffers them per id and replays the matching ones in order
// once `adopt(id)` supplies the answer.
//
// The ordering guarantee rests on `adopt()` doing replay-then-swap with no
// `await` inside it. JS is single-threaded and Tauri delivers events as
// queued tasks, so no event callback can interleave with a synchronous
// block — a chunk therefore cannot be written live before an earlier
// buffered chunk has been replayed.

/// Safety valve against unbounded growth for ids that are never adopted (a
/// pane buffers every pty's output until it learns its own id). The window is
/// milliseconds in practice; at 4 KiB per chunk this caps a stalled pane at
/// ~1 MiB per id.
export const MAX_PENDING_CHUNKS_PER_ID = 256;

export interface PtyStreamOptions {
  /** Called with output belonging to the adopted id, always in arrival order. */
  write: (data: string) => void;
  /** Called once, when the adopted id's pty has exited. */
  onExit: () => void;
}

export interface PtyStream {
  /** Feed in a `pty-output` event for any id. */
  output: (id: number, data: string) => void;
  /** Feed in a `pty-exit` event for any id. */
  exit: (id: number) => void;
  /** Declare which id is ours; replays that id's buffered output in order. */
  adopt: (id: number) => void;
  /** The adopted id, or null before `adopt()`. Exposed for assertions. */
  adoptedId: () => number | null;
}

export function createPtyStream({ write, onExit }: PtyStreamOptions): PtyStream {
  let adopted: number | null = null;
  const pending = new Map<number, string[]>();
  const exitedEarly = new Set<number>();

  function output(id: number, data: string): void {
    if (adopted !== null) {
      if (id === adopted) write(data);
      return;
    }
    let chunks = pending.get(id);
    if (!chunks) {
      chunks = [];
      pending.set(id, chunks);
    }
    chunks.push(data);
    // Overflow drops from the front, keeping the most recent output. A
    // terminal's correctness is in its latest screen state, so if this ever
    // trips it is better to lose old scrollback than the current frame.
    while (chunks.length > MAX_PENDING_CHUNKS_PER_ID) chunks.shift();
  }

  function exit(id: number): void {
    if (adopted !== null) {
      if (id === adopted) onExit();
      return;
    }
    exitedEarly.add(id);
  }

  function adopt(id: number): void {
    if (adopted !== null) return;
    // Replay, then swap, with nothing awaited in between — see the ordering
    // note at the top of this file.
    const buffered = pending.get(id);
    if (buffered) for (const chunk of buffered) write(chunk);
    adopted = id;
    pending.clear();
    // A pty that exited before we learned its id still has to report it; the
    // exit is delivered after its output, never before.
    if (exitedEarly.has(id)) onExit();
    exitedEarly.clear();
  }

  return { output, exit, adopt, adoptedId: () => adopted };
}
