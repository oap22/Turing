// One-click runners the Launcher spawns into a new terminal pane (`TermPane`
// pre-types the command for `autorun: false` entries instead of running it).
// `~` is left literal — the shell expands it for `command`, and Rust's
// `pty_spawn` expands it for `cwd` (see `expand_home` in `pty.rs`).
//
// The table is static, but the watched results directory is not: it is the
// `results` root from `app_config`, which the user may repoint at any project.
// So entries write `<RESULTS_ROOT>` and `resolveRunner()` substitutes the
// configured path at spawn time — the same placeholder trick `<SSH_HOST>` /
// `<REMOTE_RUN_DIR>` use, except this one is filled in for the user rather
// than by them.

import { inv, isTauri } from "./tauri";

export interface Runner {
  id: string;
  label: string;
  command: string;
  cwd?: string;
  group: "verify" | "research" | "remote" | "docs";
  autorun: boolean;
}

const REPO = "~/Developer/active/Turing";

/** Stands in for the configured `results` root until `resolveRunner()` runs. */
export const RESULTS_ROOT_TOKEN = "<RESULTS_ROOT>";

// Mirrors `default_roots()` in `desktop/src-tauri/src/config.rs`; used when
// there is no Tauri runtime (tests, browser) or the command fails.
const DEFAULT_RESULTS_ROOT = "~/research-results";

export const RUNNERS: readonly Runner[] = [
  {
    id: "pytest",
    label: "pytest",
    command: 'source .venv/bin/activate && pytest tests/ -m "not slow"',
    cwd: REPO,
    group: "verify",
    autorun: true,
  },
  {
    id: "pytest-full",
    label: "pytest (full)",
    command: "source .venv/bin/activate && pytest tests/ -v",
    cwd: REPO,
    group: "verify",
    autorun: true,
  },
  {
    id: "ruff",
    label: "ruff",
    command: "source .venv/bin/activate && ruff check src/ tests/",
    cwd: REPO,
    group: "verify",
    autorun: true,
  },
  {
    id: "mypy",
    label: "mypy",
    command: "source .venv/bin/activate && mypy src/",
    cwd: REPO,
    group: "verify",
    autorun: true,
  },
  {
    id: "webui-test",
    label: "webui test",
    command: "npm run test",
    cwd: `${REPO}/webui`,
    group: "verify",
    autorun: true,
  },
  {
    id: "rosie-preflight",
    label: "rosie preflight",
    command:
      "ssh -o BatchMode=yes -o ConnectTimeout=5 ROSIE 'echo ok' && ssh ROSIE 'sinfo -s; squeue -u $USER'",
    group: "remote",
    autorun: true,
  },
  {
    id: "rosie-queue",
    label: "rosie queue",
    command: "ssh ROSIE 'squeue -u $USER'",
    group: "remote",
    autorun: true,
  },
  // Host-generic on purpose: `<SSH_HOST>` and `<REMOTE_RUN_DIR>` are
  // placeholders, which is why both stream runners are `autorun: false` —
  // TermPane pre-types the command and the user edits host + path before
  // pressing Enter. ROSIE is just one example alias from `~/.ssh/config`.
  {
    id: "ssh-follow-metrics",
    label: "follow remote metrics (ssh)",
    command:
      "ssh <SSH_HOST> 'tail -n +1 -F <REMOTE_RUN_DIR>/metrics.jsonl' | tee -a <RESULTS_ROOT>/rosie-live/metrics.jsonl",
    group: "remote",
    autorun: false,
  },
  // Mirrors remote plots/metrics into the watched results dir every 30s, so
  // the images and metrics panes keep updating while a cluster job runs.
  {
    id: "ssh-pull-assets",
    label: "pull remote assets (ssh)",
    command:
      "while true; do rsync -az --include='*/' --include='*.png' --include='*.svg' --include='*.json*' --include='*.log' --exclude='*' <SSH_HOST>:<REMOTE_RUN_DIR>/ <RESULTS_ROOT>/rosie-live/; sleep 30; done",
    group: "remote",
    autorun: false,
  },
  {
    id: "vault-vim",
    label: "vault (vim)",
    command: "vim .",
    cwd: "~/Owen's Awesome Vault",
    group: "docs",
    autorun: true,
  },
  {
    id: "research-notes",
    label: "research notes (vim)",
    command: "vim JOURNAL.md",
    cwd: `${REPO}/research`,
    group: "docs",
    autorun: true,
  },
  {
    id: "research-results",
    label: "research results (vim)",
    command: "vim .",
    cwd: RESULTS_ROOT_TOKEN,
    group: "docs",
    autorun: true,
  },
];

// One lookup per app run: the roots are read from disk once at startup on the
// Rust side, so re-asking per spawned pane buys nothing.
let resultsRootPromise: Promise<string> | null = null;

interface AppConfigView {
  roots: { id: string; path: string }[];
}

export function resultsRoot(): Promise<string> {
  if (!resultsRootPromise) {
    resultsRootPromise = (async () => {
      if (!isTauri()) return DEFAULT_RESULTS_ROOT;
      try {
        const cfg = await inv<AppConfigView>("app_config");
        return cfg.roots.find((r) => r.id === "results")?.path ?? DEFAULT_RESULTS_ROOT;
      } catch {
        // A desktop that cannot read its own config is still more useful with
        // terminals than without them; fall back rather than fail the spawn.
        return DEFAULT_RESULTS_ROOT;
      }
    })();
  }
  return resultsRootPromise;
}

/**
 * Returns `runner` with `<RESULTS_ROOT>` replaced by the configured `results`
 * root in both `command` and `cwd`. Call this before spawning; the raw table
 * entries are not meant to reach a shell.
 */
export async function resolveRunner(runner: Runner): Promise<Runner> {
  const root = await resultsRoot();
  return {
    ...runner,
    command: runner.command.split(RESULTS_ROOT_TOKEN).join(root),
    cwd: runner.cwd?.split(RESULTS_ROOT_TOKEN).join(root),
  };
}

/** Test seam: drops the cached lookup so a fresh config can be asserted. */
export function __resetResultsRootForTests(): void {
  resultsRootPromise = null;
}
