// Local PTY terminal pane: xterm.js on the front, `portable-pty` via Rust on
// the back (`pty.rs`). Not covered by the jsdom test suite — `@xterm/xterm`
// stays imported only here per the spec's jsdom constraint.

import { useEffect, useRef, useState } from "react";
import { CanvasAddon } from "@xterm/addon-canvas";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { actionFor, PANE_FOCUS_EVENT, type PaneFocusDetail } from "../keymap";
import { createPtyStream } from "../ptyStream";
import { readTermTokens } from "../theme";
import { inv, subscribe } from "../tauri";
import type { Runner } from "../runners";
import { resolveRunner, RUNNERS } from "../runners";
import { rsiRunner, type RsiParams } from "../rsi";

interface Props {
  leafId: string;
  runnerId?: string;
  rsi?: RsiParams;
  visible: boolean;
}

// Nerd Fonts ship three widths of every family, and for a terminal only one of
// them is correct:
//
//   "… Nerd Font"      (NF)  — icons drawn at their natural ~2-cell width
//   "… Nerd Font Mono" (NFM) — icons squeezed to exactly one cell
//   "… Nerd Font Propo" (NFP) — proportional; not a terminal font at all
//
// xterm lays out one glyph per fixed-width cell and repaints damaged cells
// individually. A glyph from the *non*-Mono variant is drawn wider than the
// cell it belongs to, so it bleeds into its neighbour; when the shell redraws
// the line (every backspace in an oh-my-posh prompt does this) xterm repaints
// only the cells it believes changed and the bled-over pixels are left behind.
// That is precisely the reported ghosting/smearing on delete. xterm cannot
// paper over it either — its own docs for `rescaleOverlappingGlyphs` state
// that Powerline and Nerd Font glyphs "will never be rescaled", i.e. xterm
// assumes you supplied a variant whose icons already fit one cell.
//
// So the stack must lead with the *Mono* variants. Both spellings of each
// family are listed because Nerd Font v3 puts the abbreviated name in the
// font's Family record (nameID 1, e.g. "JetBrainsMonoNL NFM") and the long
// name only in the Typographic Family record (nameID 16, "JetBrainsMonoNL
// Nerd Font Mono"); which of the two a given WebKit build matches on is not
// something worth betting the prompt on. Plain "JetBrains Mono" stays as a
// late fallback for machines that have it, ahead of the UI's own
// `--font-mono` (which itself ends in ui-monospace/monospace).
const TERM_FONT_STACK =
  '"JetBrainsMonoNL Nerd Font Mono", "JetBrainsMonoNL NFM", "JetBrainsMono Nerd Font Mono", "JetBrainsMono NFM", "MesloLGS NF", "JetBrains Mono", ';

// xterm's default DOM renderer paints each cell as a positioned <span> and
// sizes the grid from a width cache built by measuring glyphs in a hidden
// container. Both halves of that are known-fragile: the cache mismeasures
// glyphs whenever the browser applies shaping inside the measurement
// container (xterm.js discussion #5155 — a mismeasured advance desyncs the
// grid so cells overlap and leave residue), Nerd Font glyphs are cut off
// outright (#3807, cross-filed as microsoft/vscode#148857), and cells that
// held a wide character are not reliably cleared when it is removed (#1035 —
// literally "characters stay behind when deleting"). The DOM renderer also
// cannot use `customGlyphs`, so it has no mitigation for the powerline
// separators an oh-my-posh prompt is built from. Moving off it is the fix
// that actually applies here.
//
// Canvas rather than WebGL, deliberately. WebGL is faster and is what VS Code
// ships, but xterm.js #5816 — full-screen WebGL rendering corruption on
// WebKit, filed with a *Tauri* reproduction, reproduced on shipping macOS —
// is still open. The root cause (stale texture bindings after glyph-atlas
// page merges) was fixed in PR #5883, which is on the 7.0.0 line only; no
// stable `@xterm/addon-webgl` release compatible with xterm 5.x contains it,
// and Safari's canvas-fingerprinting countermeasures make WebKit extra prone
// to tripping it. The upstream workaround on that issue is "switch to the
// canvas addon", so that is what we do. Canvas is GPU-composited 2D, owns a
// real pixel surface, clears a cell's full rect before redrawing, and
// supports `customGlyphs` — everything this pane needs.
//
// Must be called *after* term.open(): the addon attaches to the screen
// element open() creates and reads its computed font metrics.
function attachRenderer(term: Terminal): void {
  try {
    term.loadAddon(new CanvasAddon());
  } catch {
    // Canvas2D unavailable or the addon threw while activating. Leaves the
    // built-in DOM renderer in place — degraded, but a working terminal
    // beats a blank pane.
  }
}

function xtermTheme() {
  const tokens = readTermTokens();
  return {
    background: tokens.bg,
    foreground: tokens.fg,
    cursor: tokens.accent,
    selectionBackground: tokens.edge,
  };
}

export default function TermPane({ leafId, runnerId, rsi, visible }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const idRef = useRef<number | null>(null);
  // Every pane in a non-active workspace mounts hidden (display:none, a
  // zero-size container). Calling term.open()/fit() against that computes
  // cell metrics from a 0×0 box and never self-corrects, which is what
  // produced stale glyphs and flicker on delete. So `open()` is deferred
  // until the pane's first visible render (see the `visible` effect below)
  // — xterm buffers `term.write()` calls made before `open()` and flushes
  // them once it opens, so output arriving while still hidden isn't lost.
  const openedRef = useRef(false);
  // True once this effect's Terminal has been disposed. Output and fits can
  // still arrive asynchronously afterwards (a pending PTY chunk, a queued
  // animation frame), and both throw against a disposed Terminal — which is
  // where the unhandled `_renderer.value.dimensions` rejections came from.
  const disposedRef = useRef(false);
  const [dead, setDead] = useState(false);

  useEffect(() => {
    const tableRunner: Runner | undefined = rsi
      ? rsiRunner(rsi)
      : runnerId
        ? RUNNERS.find((r) => r.id === runnerId)
        : undefined;

    const cssMonoVar = getComputedStyle(document.documentElement)
      .getPropertyValue("--font-mono")
      .trim();
    const term = new Terminal({
      fontFamily: TERM_FONT_STACK + cssMonoVar,
      fontSize: 13,
      scrollback: 10000,
      // Draw box-drawing/block glyphs as vectors instead of trusting the
      // font, so powerline separators join up cleanly. Defaults to true but
      // is only honoured by the canvas/webgl renderers, so it is worth
      // stating explicitly now that one is attached.
      customGlyphs: true,
      // A non-zero value widens the cell without widening the glyph, which
      // reintroduces exactly the overhang-and-smear this pane is fixing.
      letterSpacing: 0,
      // Left at 1 (no dynamic contrast adjustment): raising it makes the
      // renderer cache a separate glyph per fg/bg pair, multiplying
      // glyph-atlas entries for no benefit on an already-legible theme.
      minimumContrastRatio: 1,
      theme: xtermTheme(),
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    termRef.current = term;
    fitRef.current = fit;
    // Refs outlive this effect, but the Terminal they describe does not.
    // StrictMode's dev double-mount runs cleanup (which disposes the first
    // Terminal) and then re-runs this effect, constructing a second one — so
    // both flags must be re-armed here. Leaving `openedRef` true from the
    // first mount was its own blank-pane bug: the `visible` effect would see
    // "already opened", skip `term.open()` on the brand-new Terminal, and the
    // pane would never attach to the DOM at all.
    openedRef.current = false;
    disposedRef.current = false;

    term.attachCustomKeyEventHandler((e) => {
      // No pane-local ⌘ chord may shadow the keymap. DesktopShell listens on
      // `window` with capture:true and preventDefault/stopPropagation's every
      // chord `actionFor` recognizes, so it wins before xterm's own listener
      // on its hidden textarea ever sees the event — a handler here for e.g.
      // ⌘k (once wired to clear the buffer) is simply unreachable. Clearing a
      // terminal is the shell's job anyway: ⌃l, or `clear`. ⌃/⌥ chords are
      // deliberately left unclaimed by the keymap and pass straight through.
      if (
        actionFor({
          metaKey: e.metaKey,
          shiftKey: e.shiftKey,
          ctrlKey: e.ctrlKey,
          altKey: e.altKey,
          key: e.key,
        })
      ) {
        return false;
      }
      if (e.metaKey && !e.ctrlKey && !e.altKey && e.key === "c" && e.type === "keydown") {
        const sel = term.getSelection();
        if (sel) {
          void navigator.clipboard.writeText(sel);
          return false;
        }
      }
      if (e.metaKey && !e.ctrlKey && !e.altKey && e.key === "v" && e.type === "keydown") {
        void navigator.clipboard.readText().then((text) => {
          if (idRef.current !== null) void inv("pty_write", { id: idRef.current, data: text });
        });
        return false;
      }
      return true;
    });

    let cancelled = false;

    // Holds output arriving before `pty_spawn` tells us our id, then replays
    // it in order. See ptyStream.ts — without this the whole prompt burst is
    // emitted into a window where nothing is listening yet and is dropped.
    const stream = createPtyStream({
      write: (data) => {
        if (!disposedRef.current) term.write(data);
      },
      onExit: () => {
        if (disposedRef.current) return;
        term.write("\r\n[exited]");
        setDead(true);
      },
    });

    // Subscribe FIRST, and synchronously, so no event can be emitted into a
    // window where nothing is listening. `subscribe` returns immediately while
    // the shared listener attaches; `ready` below is what guarantees the
    // attach completed before we ask Rust to spawn anything. Unsubscribing is
    // purely local, so this pane's StrictMode first-mount teardown cannot
    // disturb the remount's stream.
    const outputSub = subscribe<{ id: number; data: string }>("pty-output", (p) =>
      stream.output(p.id, p.data),
    );
    const exitSub = subscribe<{ id: number; code: number | null }>("pty-exit", (p) =>
      stream.exit(p.id, p.code),
    );

    async function boot() {
      await Promise.all([outputSub.ready, exitSub.ready]);
      if (cancelled) return;

      // Table entries carry a `<RESULTS_ROOT>` placeholder; substitute the
      // configured root before anything reaches a shell or a cwd.
      const runner = tableRunner ? await resolveRunner(tableRunner) : undefined;
      if (cancelled) return;

      const cols = term.cols;
      const rows = term.rows;
      const id = await inv<number>("pty_spawn", {
        cols,
        rows,
        command: runner?.autorun ? runner.command : undefined,
        cwd: runner?.cwd,
      });
      if (cancelled) {
        void inv("pty_kill", { id });
        return;
      }
      idRef.current = id;
      // Claim our id and flush anything that arrived while spawn was in
      // flight, before any further await — so replayed and live output
      // cannot interleave.
      stream.adopt(id);

      // The pty was spawned eagerly, possibly using stale pre-open/pre-fit
      // cols/rows (the pane may not have been open()'d yet, or may have
      // been opened and fit() since spawn started). Re-sync once we have an
      // id and current terminal metrics, regardless of which raced which —
      // the `visible` effect below also resyncs on every later show.
      void inv("pty_resize", { id, cols: term.cols, rows: term.rows });

      if (runner && !runner.autorun) {
        await inv("pty_write", { id, data: runner.command });
      }
    }
    void boot();

    term.onData((data) => {
      if (idRef.current !== null) void inv("pty_write", { id: idRef.current, data });
    });

    function onThemeChange() {
      term.options.theme = xtermTheme();
    }
    document.addEventListener("themechange", onThemeChange);

    return () => {
      // `cancelled` also covers unmounting mid-boot: boot() re-checks it after
      // every await and kills a pty that spawn returns too late, so teardown
      // is correct whether or not spawn ever resolved.
      cancelled = true;
      // Flip before dispose: async output can still be in flight, and
      // term.write() on a disposed Terminal throws.
      disposedRef.current = true;
      document.removeEventListener("themechange", onThemeChange);
      outputSub.unsubscribe();
      exitSub.unsubscribe();
      if (idRef.current !== null) void inv("pty_kill", { id: idRef.current });
      term.dispose();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Open lazily on the first visible render (see the comment on `openedRef`
  // above), and on every hidden→visible transition thereafter, fit() and
  // push the corrected size to the pty — this is what actually fixes stale
  // glyphs/flicker: the ResizeObserver below only fires on container size
  // *changes*, not on a display:none → flex transition with an unchanged
  // box size, so it can't be relied on alone to resync after a workspace
  // switch.
  //
  // xterm's DOM renderer doesn't finish attaching synchronously inside
  // open() — calling fit() in the same tick can throw
  // ("_renderer.value.dimensions" is undefined) before the renderer's first
  // paint. Deferring the *first* post-open fit to the next animation frame
  // avoids that; every fit() call is additionally wrapped so a renderer
  // hiccup can't crash the pane.
  function safeFit(term: Terminal, fit: FitAddon) {
    // A queued frame can outlive the Terminal under StrictMode's
    // mount→cleanup→remount; fitting a disposed Terminal reaches into a
    // torn-down renderer.
    if (disposedRef.current) return;
    try {
      fit.fit();
    } catch {
      return;
    }
    if (idRef.current !== null) {
      void inv("pty_resize", { id: idRef.current, cols: term.cols, rows: term.rows });
    }
  }

  useEffect(() => {
    if (!visible) return;
    const el = containerRef.current;
    const term = termRef.current;
    const fit = fitRef.current;
    if (!el || !term || !fit || disposedRef.current) return;
    if (!openedRef.current) {
      // open() and the renderer swap both touch the DOM and the renderer
      // service. If either throws we must not mark the pane opened, or the
      // remount would skip attaching entirely and leave it blank forever.
      try {
        term.open(el);
        // Swap the DOM renderer out for canvas before the first fit, so the
        // fit measures the grid the canvas renderer will actually paint.
        attachRenderer(term);
      } catch {
        return;
      }
      openedRef.current = true;
      const raf = requestAnimationFrame(() => safeFit(term, fit));
      return () => cancelAnimationFrame(raf);
    }
    safeFit(term, fit);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible]);

  // Focus-follow: the shell asks the pane that just became the focused leaf
  // to take real keyboard focus, so typing works immediately after ⌘hjkl /
  // ⌘Return / a workspace switch with no click. A terminal's focus target is
  // xterm's own hidden textarea, which is why the shell can't just call
  // .focus() on our container.
  useEffect(() => {
    function onPaneFocus(e: Event) {
      const detail = (e as CustomEvent<PaneFocusDetail>).detail;
      if (detail?.leafId !== leafId) return;
      const term = termRef.current;
      // Not yet attached (still hidden) or already torn down: nothing to
      // focus, and focus() would reach into a missing renderer.
      if (!term || !openedRef.current || disposedRef.current) return;
      try {
        term.focus();
      } catch {
        // Non-fatal: a pane that can't take focus is still usable by click.
      }
    }
    window.addEventListener(PANE_FOCUS_EVENT, onPaneFocus);
    return () => window.removeEventListener(PANE_FOCUS_EVENT, onPaneFocus);
  }, [leafId]);

  useEffect(() => {
    if (!visible) return;
    const el = containerRef.current;
    if (!el) return;
    const observer = new ResizeObserver(() => {
      const fit = fitRef.current;
      const term = termRef.current;
      if (!fit || !term || !openedRef.current) return;
      safeFit(term, fit);
    });
    observer.observe(el);
    return () => observer.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible]);

  return (
    <div className="flex h-full flex-col">
      <div ref={containerRef} className="min-h-0 flex-1" data-testid={`term-${dead ? "dead" : "live"}`} />
    </div>
  );
}
