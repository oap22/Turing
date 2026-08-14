// Plain-text view of a pty byte stream, for the update overlay's build log.
//
// The overlay is a log, not a terminal: it never sends input and nothing
// interactive runs in it, so mounting a whole xterm instance (renderer,
// glyph atlas, fit dance — see TermPane) buys nothing. But the stream still
// *looks* like terminal output — git and cargo colorize with ANSI sequences
// and animate progress with bare `\r` rewrites — so a naive `<pre>` append
// shows escape garbage and one progress line duplicated hundreds of times.
//
// This model strips the styling and applies `\r` as an overwrite, which is
// exactly enough to make a build log readable:
//
//   - complete ANSI sequences (CSI, OSC, 2-byte ESC) are removed
//   - a sequence split across chunk boundaries is carried, like the UTF-8
//     carry in Rust's `split_utf8` one layer down
//   - `\r` returns the cursor to column 0; what follows overwrites the line
//     from the start, leaving any longer remainder in place ("abc" + \r"X"
//     renders "Xbc", matching a real terminal)
//   - other control characters are dropped (keeping `\t`)
//
// What it deliberately does not do: cursor addressing. A CSI that moves the
// cursor is stripped rather than honoured, so full-screen redraws would
// render as appended text — fine for a build log, wrong for vim, which is
// why TermPane keeps xterm and this does not replace it.

/// Longest tail held back waiting for an escape-sequence terminator. An OSC
/// (window title, hyperlink) can legitimately run long, but an unterminated
/// one must not buffer the stream forever — past this, it is flushed as text
/// (the stripper drops whatever part of it still parses).
const MAX_CARRY = 1024;

/// Oldest lines are dropped past this. A full desktop build logs a few
/// thousand lines; the cap only exists so a looping child cannot grow the
/// overlay's memory without bound.
const MAX_LINES = 5000;

const ESC = "\x1b";

// Complete-sequence matchers, anchored at an ESC. CSI covers colors and
// cursor movement; OSC (terminated by BEL or ST) covers titles and
// hyperlinks; the 2-byte class covers the remaining Fe escapes.
const COMPLETE_CSI = /^\x1b\[[0-9;:?]*[ -/]*[@-~]/;
const COMPLETE_OSC = /^\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/;
const COMPLETE_TWO_BYTE = /^\x1b[@-Z\\^_]/;

function isCompleteSequenceAt(tail: string): boolean {
  return (
    COMPLETE_CSI.test(tail) || COMPLETE_OSC.test(tail) || COMPLETE_TWO_BYTE.test(tail)
  );
}

/** Strip complete ANSI sequences and control chars other than \t, \r, \n. */
export function stripAnsi(text: string): string {
  return (
    text
      .replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, "")
      .replace(/\x1b\[[0-9;:?]*[ -/]*[@-~]/g, "")
      .replace(/\x1b[@-Z\\^_]/g, "")
      .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "")
  );
}

/**
 * `\r` semantics on a single line: each segment after a `\r` overwrites the
 * accumulated line from column 0, keeping any longer remainder.
 */
export function applyCarriageReturns(line: string): string {
  const segments = line.split("\r");
  let acc = segments[0];
  for (let i = 1; i < segments.length; i++) {
    acc = segments[i] + acc.slice(segments[i].length);
  }
  return acc;
}

export interface UpdateLog {
  /** Feed a raw pty chunk. Returns true when the visible text changed. */
  feed: (chunk: string) => boolean;
  /** Current lines, oldest first; the last entry is the in-progress line. */
  lines: () => readonly string[];
}

export function createUpdateLog(): UpdateLog {
  const done: string[] = [];
  // The in-progress line keeps its raw `\r`s until snapshot time, so an
  // overwrite arriving in a later chunk still lands on the same line.
  let current = "";
  let carry = "";

  function feed(chunk: string): boolean {
    let text = carry + chunk;
    carry = "";

    // Carve off a trailing escape sequence that hasn't terminated yet.
    const lastEsc = text.lastIndexOf(ESC);
    if (lastEsc !== -1 && !isCompleteSequenceAt(text.slice(lastEsc))) {
      const tail = text.slice(lastEsc);
      if (tail.length <= MAX_CARRY) {
        carry = tail;
        text = text.slice(0, lastEsc);
      }
      // Unterminated past the cap: fall through and let the stripper drop
      // whatever part of it still parses — buffering forever is worse.
    }

    text = stripAnsi(text);
    if (text === "") return false;

    const parts = text.split("\n");
    current += parts[0];
    for (let i = 1; i < parts.length; i++) {
      done.push(applyCarriageReturns(current));
      current = parts[i];
    }
    if (done.length > MAX_LINES) done.splice(0, done.length - MAX_LINES);
    return true;
  }

  function lines(): readonly string[] {
    return [...done, applyCarriageReturns(current)];
  }

  return { feed, lines };
}
