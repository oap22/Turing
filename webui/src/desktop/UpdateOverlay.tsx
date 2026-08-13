// In-app update (issue #397): run `git pull` + the install script in a pty
// and watch the log without leaving the app, then relaunch into the new
// build. This automates the existing local workflow — repo checkout and
// toolchain still required — it is not a hosted updater.
//
// The install script is invoked with `--in-place`, the flag added for this
// flow: without it the script quits the running app before installing, which
// would tear down the very pty it is writing to. Replacing the bundle under
// a running process is safe (the process keeps its inodes), and the "quit"
// half is replaced by the relaunch offered on success (`app_relaunch` →
// Tauri's request_restart, which execs the freshly installed binary).
//
// Success is judged by the shell's exit code, now carried on `pty-exit`
// (`code: Option<u32>` in pty.rs). A `null` code means Rust couldn't reap
// the child — treated as failure, never as success.

import { useEffect, useRef, useState } from "react";
import { createPtyStream } from "./ptyStream";
import { createUpdateLog } from "./updateLog";
import { inv, isTauri, subscribe } from "./tauri";

interface Props {
  onClose: () => void;
}

// `--ff-only` so a diverged checkout fails loudly here instead of leaving a
// surprise merge commit for the next `git status`. The build+install half is
// the same script the terminal workflow uses; keeping them identical means
// there is exactly one definition of "install the desktop app".
export const UPDATE_COMMAND = "git pull --ff-only && scripts/install-desktop.sh --in-place";

// Mirrors `default_roots()` in desktop/src-tauri/src/config.rs, same as
// runners.ts does for the results root; used if reading the config fails.
const DEFAULT_REPO_ROOT = "~/Developer/active/Turing";

interface AppConfigView {
  roots: { id: string; path: string }[];
}

async function repoRoot(): Promise<string> {
  try {
    const cfg = await inv<AppConfigView>("app_config");
    return cfg.roots.find((r) => r.id === "repo")?.path ?? DEFAULT_REPO_ROOT;
  } catch {
    return DEFAULT_REPO_ROOT;
  }
}

type Phase =
  | { kind: "running" }
  | { kind: "ok" }
  | { kind: "failed"; detail: string };

export default function UpdateOverlay({ onClose }: Props) {
  const [phase, setPhase] = useState<Phase>(() =>
    isTauri()
      ? { kind: "running" }
      : { kind: "failed", detail: "updating needs the desktop app (no Tauri runtime)" },
  );
  // Two-step cancel: a build is not something to kill on a stray Escape.
  const [escArmed, setEscArmed] = useState(false);
  // The log lives in a ref and renders via a tick, coalesced to animation
  // frames — a cargo build emits far more chunks than are worth rendering.
  const logRef = useRef(createUpdateLog());
  const [, setTick] = useState(0);
  const frameRef = useRef<number | null>(null);
  const idRef = useRef<number | null>(null);
  const boxRef = useRef<HTMLDivElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const phaseRef = useRef(phase);
  phaseRef.current = phase;

  useEffect(() => {
    boxRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!isTauri()) return;

    let cancelled = false;

    function scheduleRender() {
      if (frameRef.current !== null) return;
      frameRef.current = requestAnimationFrame(() => {
        frameRef.current = null;
        setTick((t) => t + 1);
      });
    }

    const stream = createPtyStream({
      write: (data) => {
        if (logRef.current.feed(data)) scheduleRender();
      },
      onExit: (code) => {
        idRef.current = null;
        setPhase(
          code === 0
            ? { kind: "ok" }
            : { kind: "failed", detail: code === null ? "process was killed" : `exit ${code}` },
        );
      },
    });

    // Subscribe before spawning, and await the attach before asking Rust to
    // spawn — the same drop-window dance TermPane does (see ptyStream.ts).
    const outputSub = subscribe<{ id: number; data: string }>("pty-output", (p) =>
      stream.output(p.id, p.data),
    );
    const exitSub = subscribe<{ id: number; code: number | null }>("pty-exit", (p) =>
      stream.exit(p.id, p.code),
    );

    async function boot() {
      await Promise.all([outputSub.ready, exitSub.ready]);
      if (cancelled) return;
      const cwd = await repoRoot();
      if (cancelled) return;
      try {
        // Fixed size: nothing here resizes, and 120 columns keeps build
        // tools' wrapped output readable in the log.
        const id = await inv<number>("pty_spawn", {
          cols: 120,
          rows: 30,
          command: UPDATE_COMMAND,
          cwd,
        });
        if (cancelled) {
          void inv("pty_kill", { id });
          return;
        }
        idRef.current = id;
        stream.adopt(id);
      } catch (e) {
        if (!cancelled) setPhase({ kind: "failed", detail: String(e) });
      }
    }
    void boot();

    return () => {
      cancelled = true;
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
      outputSub.unsubscribe();
      exitSub.unsubscribe();
      // Closing the overlay abandons the run; a build left running headless
      // would install under the user's feet with nothing showing progress.
      if (idRef.current !== null) void inv("pty_kill", { id: idRef.current });
    };
  }, []);

  const lines = logRef.current.lines();

  // Follow the tail, like a terminal would.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  });

  function onKeyDown(e: React.KeyboardEvent) {
    // The overlay owns the keyboard while open; nothing may leak to Home.
    e.stopPropagation();
    if (e.key === "Escape") {
      e.preventDefault();
      if (phaseRef.current.kind !== "running") {
        onClose();
        return;
      }
      if (!escArmed) {
        setEscArmed(true);
        return;
      }
      if (idRef.current !== null) void inv("pty_kill", { id: idRef.current });
      onClose();
      return;
    }
    if (escArmed) setEscArmed(false);
    if (e.key === "Enter" && phaseRef.current.kind === "ok") {
      e.preventDefault();
      // Fire and forget: on success the process is about to be replaced, so
      // no response ever arrives.
      void inv("app_relaunch").catch(() => {});
    }
  }

  const status =
    phase.kind === "running"
      ? escArmed
        ? "cancel the update? esc again to confirm · any other key to keep going"
        : "updating — pull, build, install · esc cancel"
      : phase.kind === "ok"
        ? "update installed ✓ · return relaunch now · esc relaunch later"
        : `update failed (${phase.detail}) · esc close`;

  return (
    <div
      ref={boxRef}
      role="dialog"
      aria-label="Update"
      tabIndex={-1}
      onKeyDown={onKeyDown}
      className="absolute inset-0 z-50 flex flex-col items-center justify-center bg-term-bg p-6 focus:outline-none"
    >
      <div className="flex h-full max-h-[80vh] w-full max-w-4xl flex-col border border-term-edge bg-term-panel">
        <div className="flex items-baseline gap-3 border-b border-term-edge px-3 py-2 text-[10px] uppercase tracking-widest text-term-dim">
          <span>update</span>
          <span className="ml-auto max-w-[60%] truncate font-mono normal-case tracking-normal">
            {UPDATE_COMMAND}
          </span>
        </div>
        <div
          ref={scrollRef}
          data-testid="update-log"
          className="min-h-0 flex-1 overflow-auto px-3 py-2"
        >
          <pre className="whitespace-pre-wrap break-words font-mono text-[11px] leading-snug text-term-fg">
            {lines.join("\n")}
          </pre>
        </div>
        <div
          data-testid="update-status"
          className={`border-t border-term-edge px-3 py-2 text-[10px] ${
            phase.kind === "failed" ? "text-term-accent" : "text-term-dim"
          }`}
        >
          {status}
        </div>
      </div>
    </div>
  );
}
