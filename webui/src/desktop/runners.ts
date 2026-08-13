// One-click runners the Launcher spawns into a new terminal pane (`TermPane`
// pre-types the command for `autorun: false` entries instead of running it).
// `~` is left literal — the shell expands it for `command`, and Rust's
// `pty_spawn` expands it for `cwd` (see `expand_home` in `pty.rs`).

export interface Runner {
  id: string;
  label: string;
  command: string;
  cwd?: string;
  group: "verify" | "research" | "remote" | "docs";
  autorun: boolean;
}

const REPO = "~/Developer/active/Turing";

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
      "ssh <SSH_HOST> 'tail -n +1 -F <REMOTE_RUN_DIR>/metrics.jsonl' | tee -a ~/Developer/active/Turing/research/results/rosie-live/metrics.jsonl",
    group: "remote",
    autorun: false,
  },
  // Mirrors remote plots/metrics into the watched results dir every 30s, so
  // the images and metrics panes keep updating while a cluster job runs.
  {
    id: "ssh-pull-assets",
    label: "pull remote assets (ssh)",
    command:
      "while true; do rsync -az --include='*/' --include='*.png' --include='*.svg' --include='*.json*' --include='*.log' --exclude='*' <SSH_HOST>:<REMOTE_RUN_DIR>/ ~/Developer/active/Turing/research/results/rosie-live/; sleep 30; done",
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
    cwd: `${REPO}/research/results`,
    group: "docs",
    autorun: true,
  },
];
