# turing-desktop

A Tauri 2 shell that hosts the `webui` React SPA and turns it into a
keyboard-first, Hyprland/omarchy-style tiling operator surface: 5 numbered
workspaces of binary-split panes, local PTY terminals, one-click runners,
live metrics/flywheel/agent panes, and an authenticated Rust-side proxy to
the `turing-gateway` so Queue/Chat/Observability stay live outside the
browser. See issue #382 and `.plan-then-ship/SPEC.md` for the full design.

It runs either from source (`npm run dev`) or as an installed
`/Applications/turing.app` — see [Packaging](#packaging).

## Prereqs

- Rust + Cargo (stable), matching the versions the rest of the repo builds
  with.
- Node (the same version `webui/` uses) with the `webui/` dependencies
  installed:

  ```bash
  cd webui && npm ci
  ```

## Run

```bash
cd desktop
npm ci
npm run dev     # = `tauri dev`; boots the webui Vite dev server, then the window
```

## Updating the installed app

One command, from anywhere in the repo:

```bash
npm --prefix desktop run install-app
```

That runs `scripts/install-desktop.sh`, which builds (`tauri build`, which
chains the `webui` build itself), quits a running `turing.app` if one is
open, replaces `/Applications/turing.app`, re-signs it ad-hoc, clears
`com.apple.quarantine` if present, and prints the installed version. No
`sudo` — if `/Applications` isn't writable it stops and tells you what to
run. Flags (pass them after `--`):

```bash
npm --prefix desktop run install-app -- --no-build   # reinstall the last build
npm --prefix desktop run install-app -- --open       # launch when done
```

The install path builds `--bundles app` only. The `.dmg` is not needed to
install locally, and its bundling step fails outright when an earlier
interrupted build has left a `/Volumes/dmg.*` volume mounted — clear one with
`diskutil eject /Volumes/dmg.<id>` if you hit it while building a dmg
deliberately.

The app is **ad-hoc signed**, not notarized (details under
[Packaging](#packaging)). Locally built, it carries no quarantine attribute,
so Gatekeeper stays out of the way; a copy moved to another Mac *is*
quarantined there and needs a one-time right-click → Open.

## Packaging

Manual equivalent of the above, if you want the steps separately:

```bash
cd desktop
npm run build                                    # = `tauri build`
rm -rf /Applications/turing.app
cp -R src-tauri/target/release/bundle/macos/turing.app /Applications/
codesign --force --deep --sign - /Applications/turing.app
```

`tauri build` produces both bundles under `src-tauri/target/release/bundle/`:
`macos/turing.app` and `dmg/turing_<version>_aarch64.dmg`. The app is
**ad-hoc signed** (`bundle.macOS.signingIdentity: "-"`) and not notarized —
Owen is the only user, and a locally built app carries no
`com.apple.quarantine` attribute, so Gatekeeper never gates it. `spctl -a`
reports `rejected` for exactly that reason; it is expected, not a failure.
The `.dmg` exists for moving the app to another Mac — do that and macOS
*will* quarantine it, so clear it there with
`xattr -dr com.apple.quarantine /Applications/turing.app`.

The `build` script drops a `.metadata_never_index` marker in `target/`
before bundling, so Spotlight doesn't index the freshly built
`bundle/macos/turing.app` alongside the installed one — without it, ⌘Space
and the launcher offer two identical `turing` apps. The marker itself can't
be committed (`target/` is gitignored), which is why the script recreates
it on every build.

Bundling is on for macOS only in practice; the config carries no
Windows/Linux-specific bundle settings.

### Icon

The mark is a bracketed capital T — `[T]` — in the default `turing` palette
(`--t-bg` plate, `--t-fg` brackets, `--t-accent` letter), chosen to stay
legible down to the 32px Finder size.

`src-tauri/icons/make_icon.py` is the single source of truth: geometry
constants at the top drive both `icon.svg` (vector master) and
`icon-source.png` (the 1024px raster the icon set is derived from). To
change the mark, edit the constants and regenerate:

```bash
cd desktop/src-tauri/icons
python3 make_icon.py
cd ../.. && npx tauri icon src-tauri/icons/icon-source.png
rm -rf src-tauri/icons/ios src-tauri/icons/android \
       src-tauri/icons/Square*Logo.png src-tauri/icons/StoreLogo.png
```

That last `rm` drops the iOS/Android/Windows-Store variants `tauri icon`
emits unconditionally; this is a desktop-only app and `bundle.icon` lists
only the five files it actually uses. The script needs Pillow
(`pip install pillow`) — it draws the geometry directly rather than
rasterizing the SVG, because no SVG rasterizer is assumed present and
macOS' `qlmanage` bakes a drop shadow into its output.

## Config

Optional overlay at `~/.config/turing-desktop/config.json` — any key may be
omitted to keep the default; a missing file or parse error silently falls
back to defaults (nothing is ever written by the app). `roots`, when
present, replaces the default root list wholesale.

```json
{
  "gateway_base": "http://127.0.0.1:8765",
  "gateway_token": "",
  "roots": [
    { "id": "vault", "path": "~/Owen's Awesome Vault" },
    { "id": "results", "path": "~/research-results" },
    { "id": "repo", "path": "~/Developer/active/Turing" },
    { "id": "claude-sessions", "path": "~/.claude/projects" },
    { "id": "codex-sessions", "path": "~/.codex/sessions" }
  ]
}
```

The gateway token, if set, never reaches the webview — Rust attaches it to
proxied requests and the WS handshake; the frontend only ever sees
`gateway_token_set: bool` via the `app_config` command.

The `results` root is deliberately **not** inside the Turing checkout: any
project (`~/Developer/active/mnist`, a scratch notebook, a cluster mirror) can
write `<run-id>/metrics.jsonl` under `~/research-results/` and light up the
metrics, images and flywheel panes, without leaving untracked artifacts in a
repo. Repoint it here if you keep results elsewhere; keep the id `results`,
which those panes look up by name.

## Keymap

Mod is ⌘ alone (⌃/⌥ stay free for terminal apps). Two bindings moved off the
plain omarchy defaults to avoid a collision; the in-app cheatsheet (⌘/)
documents this too.

| Keys | Action |
|---|---|
| ⌘ Return | new terminal |
| ⌘ w | close focused pane |
| ⌘ hjkl / arrows | move focus |
| ⌘ shift + hjkl / arrows | swap focused pane with the neighbor |
| ⌘ 1..5 | switch workspace |
| ⌘ shift + 1..5 | send focused pane to a workspace (you stay put) |
| ⌘ f | toggle zoom (focused pane full-size) |
| ⌘ t | toggle split direction *(moved off ⌘j — collides with focus-down)* |
| ⌘ - / ⌘ = | shrink / grow focused pane |
| ⌘ p | launcher (panes + runners, fuzzy filter) |
| ⌘ / | cheatsheet *(moved off ⌘k — collides with focus-up)* |

> in a focused terminal, ⌘k clears the buffer — use arrows/⌘↑ to focus upward from a terminal

Focus follows the tiling focus, Hyprland-style: any keyboard action that moves
focus (the six rows above through ⌘shift 1..5, plus opening a pane from the
launcher) gives the newly focused pane real keyboard focus — a terminal starts
accepting keystrokes with no click — and warps the mouse pointer to that pane's
centre. Clicking a pane focuses it too, without moving the pointer; overlays
(⌘p, ⌘/) and layout restore on launch never steal focus or the cursor.

## Themes

Omarchy-style palettes, switched from the top-bar `<select>` (persists to
`localStorage`, applies app-wide including open terminals): `turing`
(default), `tokyo-night`, `gruvbox`, `catppuccin`, `nord`, `everforest`,
`kanagawa`, `rose-pine`, `matte-black`.

## Workspace presets (first launch only)

`defaultLayout()` only seeds a brand-new install — once anything is
persisted to `localStorage["turing.layout.v2"]` these presets never apply
again, and every pane type below is always reachable via the ⌘p launcher
regardless of what a workspace starts with.

| Workspace | Preset |
|---|---|
| ⌘1 | code — two terminals, split |
| ⌘2 | train — metrics / images / flywheel |
| ⌘3 | empty |
| ⌘4 | agents — agent session list / cross-session feed |
| ⌘5 | empty |

## Panes

`term` (local shell via `portable-pty`), `queue` / `chat` / `obs` (the exact
browser-tab components, gateway-proxied), `metrics` (tails `metrics.jsonl`/
`metrics.json`, multi-run overlay, one series at a time via tabs, ETA strip),
`images` (browses result images, see below), `flywheel` (parses
`<results>/loop-*/trajectory.json` round timelines), `agents` (live
Claude Code / Codex CLI session list + transcript tail), `agentfeed`
(cross-session tool/assistant activity feed).

### The `flywheel` pane's round timeline

Each round renders as index, the cells it scored, a status glyph, and the
driving-function numbers — per-cell primary score with gain in noise units,
then cost and human-gate load. Status comes from the structured fields the
loop writes, not from the prose `verdict` string (which is appended to the
detail instead):

| Glyph | Meaning |
|---|---|
| `✓` | improving — at least one cell beat its noise floor |
| `✗` | failed a gate (`constraints`) |
| `=` | saturated — no cell beat its noise floor |
| `?` | refused — the measurement needed for the call was never made |
| `·` | no verdict, e.g. a round 0 baseline with no parent to compare against |

`?` and `=` are deliberately distinct: a refusal is not a flat round, and
collapsing them would let a missing measurement read as a real result.

### The `images` pane and follow-mode

The pane merges both watched roots newest-first and, by default, **follows the
newest image** — `[watch latest]` starts on, so a fresh plot landing under a
watched root becomes the preview. That is the right behavior while a job runs
and nothing has been picked: `ssh-pull-assets` mirrors remote plots every 30s
and the pane acts as a live wall.

**Selecting an image turns following off.** The pane distinguishes the
selection it chose from the one you chose, and an image you deliberately
selected does not change without another user action — so studying an older
plot mid-run is no longer interrupted every 30 seconds.

Getting back to following, either way:

- `[watch latest]` — off → on re-selects the newest and resumes following.
- `[N new]` — appears beside it once newer images have landed behind your
  selection; click to jump to the newest and resume.

Clicking an image enlarges it in an in-pane lightbox: `Escape` or a click on
the backdrop dismisses, `←`/`→` step through images in the same newest-first
order the list shows (which also moves the list highlight). Following stays
suspended while enlarged, so an arriving image never swaps out the one you are
looking at. The lightbox stays inside the pane's own bounds, so it composes
with `⌘f` zoom rather than fighting it — enlarging inside a zoomed pane gives
you the image full-window.

`chat` is reserved, not active work: it is held for the future Jetson-nano
fleet coordinator and stays dormant until the gateway/coordinator stack wakes.
The launcher labels it `chat — future: nano agent network` to say so. Its code
is wired and intentionally kept.

## Agent-driven viewing

The `metrics` pane also watches `.viewer.json` at the root of the results
directory. A coding
agent can write this file to point Owen's metrics pane at what it wants him
to see — the pane re-reads it on every change:

```json
{
  "series": "accuracy",
  "runs": ["demo-run"],
  "titles": { "accuracy": "Eval accuracy — sweep 7" }
}
```

It is a plain file in the results root, so writing it by hand (`vim
~/research-results/.viewer.json`) is as supported as an agent writing it; the
pane re-reads on save either way.

All keys are optional and applied independently:

- `series` — switches the active tab to this series name, exactly as
  clicking the tab would (and persists the same way, to
  `localStorage["turing.metrics.series"]`). Ignored if no run currently
  selected has a series by this name.
- `runs` — an array of run labels (the directory name under
  the results root, e.g. `"demo-run"` for
  `~/research-results/demo-run/metrics.jsonl`). Replaces the pane's run
  selection with whichever of these labels match a discovered run; labels
  that don't match anything are ignored.
- `titles` — a map of series name → display title. The chart's label row
  shows the mapped title while the series **tab keeps the raw name**, so tabs
  stay short and stable no matter how wordy a title gets. Series without an
  entry keep their raw name. The file is authoritative: removing the key
  removes the titles again.

Malformed JSON, an unknown series name, run labels that match nothing, a
`titles` value that isn't a string map, or any unrecognised key are all
ignored silently — the pane just keeps showing whatever it already had. A
half-finished hand edit therefore degrades to "show less", never to a blank
pane.

## Agent-driven pane control

The same idea one level up: `.layout.json`, also in the results root, says
**which panes exist, where, and on which workspace**. The running app
rearranges itself on save — no relaunch, and no hand-editing the persisted
layout blob in the WebKit localStorage store, which is how this had to be
done before.

```json
{
  "workspaces": {
    "3": {
      "dir": "h",
      "ratio": 0.65,
      "panes": [{ "pane": "agents" }, { "pane": "agentfeed" }]
    },
    "4": null
  },
  "active": 3
}
```

That is the motivating case in one file: set up workspace 3 for agent
observation, empty workspace 4, and switch to 3.

**The file is declarative.** It states the layout it wants; the app makes
reality match. There are no imperative "open a pane" / "close a pane"
operations — you open a pane by listing it and close one by leaving it out.

**Re-applying the same file really does nothing**, so it is safe for an agent
to rewrite on every turn. The app reconciles against the panes already on
screen: a pane whose type and `runnerId` already match keeps its identity,
and an unchanged workspace is not rebuilt. That matters because panes are
torn down and remounted when their identity changes — for a `term` that
means killing the pty and respawning its runner, so a naive rewrite-every-turn
loop would kill the test run it had just started. Changing a ratio likewise
re-splits without disturbing the panes. Your focused pane and a hand-set
`⌘f` zoom both survive a re-apply.

**Workspace keys are the numbers you press `⌘` with, `"1"` through `"5"`** —
the same numbers as the workspace table above, not 0-based indices.

**A workspace the file mentions is replaced wholesale; one it does not
mention is left completely alone.** That is what lets a file rearrange
workspace 3 without disturbing the terminals you have open on workspace 1.
`null` empties a workspace.

Per workspace, give either:

- `panes` — a flat list, folded into a spine using `dir` (`"h"` or `"v"`,
  default `h`) and `ratio` (default `0.5`); or
- `tree` — an explicit nested shape, for layouts a single spine can't
  express:

```json
{
  "workspaces": {
    "2": {
      "tree": {
        "split": "h",
        "ratio": 0.55,
        "a": { "pane": "metrics" },
        "b": {
          "split": "v",
          "ratio": 0.5,
          "a": { "pane": "images" },
          "b": { "pane": "flywheel" }
        }
      }
    }
  }
}
```

`pane` is any of the pane types listed under [Panes](#panes). Ratios are
limited to `0.1`–`0.9`, the same range dragging a divider can reach.
`active` (optional, also 1-based) switches the visible workspace.

**A `term` pane may name a `runnerId`, and only a `runnerId`** — an id from
the runner table, e.g. `{ "pane": "term", "runnerId": "ssh-pull-assets" }`.
Arbitrary command strings are deliberately not accepted: anything able to
write into the results root would otherwise have shell execution on this
machine. Launch something not in the table by adding a runner.

Be aware of what that still permits, though. Anything that can write into the
results root can cause any `autorun: true` runner to execute unattended — and
some of those, `rosie-preflight` and `rosie-queue`, run `ssh` against a remote
host. The control file is as trusted as write access to the results
directory; treat it that way, and prefer `autorun: false` for runners with
side effects beyond the local machine.

**Rearranging never steals focus and never moves the mouse pointer.** The
change is applied passively, so panes can be rearranged under your hands
while you keep typing. `active` moves which workspace is shown, but not
keyboard focus.

**Malformed input is ignored whole.** Bad JSON (including a file caught
mid-write), an unknown pane type, a workspace number out of range, a ratio
outside the allowed span, or a runner id not in the table rejects the
**entire** request and the layout stays exactly as it was — never a
half-applied tree with one workspace changed and another not. Unrecognised
*extra* keys are ignored so the format can grow.

## Runners (⌘p → type to filter)

One-click commands the launcher spawns into a new terminal pane. `~` is a
literal in every `command`/`cwd` — the shell (and, for `cwd`, `pty_spawn`)
expands it. `<RESULTS_ROOT>` is substituted with the configured `results` root
before the command reaches a shell, so these entries follow the config rather
than hardcoding a repo.

| id | group | command | cwd | autorun |
|---|---|---|---|---|
| pytest | verify | `source .venv/bin/activate && pytest tests/ -m "not slow"` | repo | true |
| pytest-full | verify | `source .venv/bin/activate && pytest tests/ -v` | repo | true |
| ruff | verify | `source .venv/bin/activate && ruff check src/ tests/` | repo | true |
| mypy | verify | `source .venv/bin/activate && mypy src/` | repo | true |
| webui-test | verify | `npm run test` | repo/webui | true |
| rosie-preflight | remote | `ssh -o BatchMode=yes -o ConnectTimeout=5 ROSIE 'echo ok' && ssh ROSIE 'sinfo -s; squeue -u $USER'` | — | true |
| rosie-queue | remote | `ssh ROSIE 'squeue -u $USER'` | — | true |
| ssh-follow-metrics | remote | `ssh <SSH_HOST> 'tail -n +1 -F <REMOTE_RUN_DIR>/metrics.jsonl' \| tee -a <RESULTS_ROOT>/rosie-live/metrics.jsonl` | — | **false** (pre-typed, not run) |
| ssh-pull-assets | remote | `while true; do rsync -az --include='*/' --include='*.png' --include='*.svg' --include='*.json*' --include='*.log' --exclude='*' <SSH_HOST>:<REMOTE_RUN_DIR>/ <RESULTS_ROOT>/rosie-live/; sleep 30; done` | — | **false** (pre-typed, not run) |
| vault-vim | docs | `vim .` | `~/Owen's Awesome Vault` | true |
| research-notes | docs | `vim JOURNAL.md` | repo/research | true |
| research-results | docs | `vim .` | results | true |

The `remote` group is host-agnostic. `<SSH_HOST>` and `<REMOTE_RUN_DIR>` are
placeholders you edit before pressing Enter — which is exactly why those two
runners are `autorun: false` and only pre-typed. `ROSIE` in the two
rosie-specific conveniences is just one example host: it is an alias from
`~/.ssh/config`, and any other alias works the same way.

`ssh-pull-assets` mirrors remote plots and metrics into
`<results>/rosie-live/` every 30s, so the `images` and `metrics` panes
keep updating live while a cluster job runs.

There is no markdown/wikilink renderer — docs are read via `vim` in a
terminal pane.

## Troubleshooting

- **Queue/chat/obs show "offline" and nothing else happens** — this is
  normal with no gateway running locally; the Rust WS proxy retries with
  backoff quietly, and HTTP calls surface a synthetic 599.
- **Vault pane / `vault-vim` runner errors** — the vault path
  (`~/Owen's Awesome Vault`) is machine-specific; fix it in
  `~/.config/turing-desktop/config.json`.
- **A `results`/`claude-sessions`/`codex-sessions` root that doesn't exist
  on this machine** — the panes that watch it just error on use; it's not
  fatal to the rest of the shell.
