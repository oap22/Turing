# turing-desktop

A Tauri 2 shell that hosts the `webui` React SPA and turns it into a
keyboard-first, Hyprland/omarchy-style tiling operator surface: numbered
workspaces (three seeded, growable up to nine with ⌘N) of binary-split
panes, local PTY terminals, one-click runners, live metrics/flywheel/agent
panes, and an authenticated Rust-side proxy to the `turing-gateway` so
Queue/Chat/Observability stay live outside the browser. See issue #382 and
`.plan-then-ship/SPEC.md` for the full design.

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

**From inside the app:** press `u` on the home screen (⌘0). That runs
`git pull --ff-only && scripts/install-desktop.sh --in-place` in a pty and
shows the build log in an overlay; on success, Return relaunches into the
new build. `--in-place` skips the script's quit-the-running-app step —
replacing the bundle under a running process is safe (it keeps its inodes),
and the relaunch is what actually picks up the new binary. This automates
the local workflow below; the repo checkout and toolchain are still
required.

**From a terminal**, one command, from anywhere in the repo:

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

`bundle.targets` names only the macOS bundles (`app`, `dmg`), so local
`tauri build` on a Mac is unaffected by other platforms' settings. The
Windows bundle is selected **per-invocation** instead of via `targets` —
`tauri build --bundles nsis` — which is what CI's `desktop-windows` job
runs; the config carries no Linux-specific bundle settings.

### Windows (NSIS)

Built by CI only (the `desktop-windows` job on `windows-latest`, issue
#399) — there is no cross-compile path from macOS. `bundle.windows` in
`tauri.conf.json` configures:

- **NSIS, per-user** (`installMode: "currentUser"`): no admin prompt,
  installs under `%LOCALAPPDATA%\turing` (Tauri's NSIS template uses
  `$LOCALAPPDATA\<productName>`), per-user uninstaller. An MSI
  is available on demand via `--bundles msi` (WiX); nothing else changes.
- **WebView2** via the Evergreen `downloadBootstrapper` (silent), for
  images that don't ship it.
- **Code-signing placeholders**: `certificateThumbprint: null`,
  `timestampUrl: null`, `digestAlgorithm: "sha256"`. The installer is
  unsigned until a certificate exists, so SmartScreen warns on first run
  ("More info → Run anyway"). Fill in a real thumbprint + RFC-3161
  timestamp URL to sign; never commit fake values.

Install/run/Defender details for operators live in
`docs/operator/windows.md`.

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

Optional overlay at `~/.config/turing-desktop/config.json` (on Windows:
`%APPDATA%\turing-desktop\config.json`) — any key may be omitted to keep
the default; a missing file or parse error silently falls back to defaults
(nothing is ever written by the app). `roots`, when present, replaces the
default root list wholesale.

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
| ⌘ w | close focused pane — or, if the workspace is empty, close the workspace itself |
| ⌘ hjkl / arrows | move focus |
| ⌘ shift + hjkl / arrows | swap focused pane with the neighbor |
| ⌘ 1..9 | switch workspace |
| ⌘ shift + 1..9 | send focused pane to a workspace (you stay put) |
| ⌘ n | new workspace (past the seeded three, up to nine) |
| ⌘ f | toggle zoom (focused pane full-size) |
| ⌘ t | toggle split direction *(moved off ⌘j — collides with focus-down)* |
| ⌘ - / ⌘ = | shrink / grow focused pane |
| ⌘ p | launcher (panes + runners, fuzzy filter) |
| ⌘ / | cheatsheet *(moved off ⌘k — collides with focus-up)* |

> Every ⌘ chord in this table belongs to the shell, in a focused terminal too:
> `DesktopShell` listens on `window` with `capture: true` and swallows any chord
> `actionFor` recognizes, so it always fires before xterm's own listener. A pane
> cannot claim one back — ⌘k focuses up even with a terminal focused. ⌃/⌥ chords
> are left unbound for exactly that reason and pass through untouched, so clear
> a terminal with ⌃l or `clear`.

Focus follows the tiling focus, Hyprland-style: any keyboard action that moves
focus (the rows above through ⌘shift 1..9, plus opening a pane from the
launcher) gives the newly focused pane real keyboard focus — a terminal starts
accepting keystrokes with no click — and warps the mouse pointer to that pane's
centre. Clicking a pane focuses it too, without moving the pointer; overlays
(⌘p, ⌘/) and layout restore on launch never steal focus or the cursor.

⌘W folds an empty *trailing* extra workspace away entirely (down to a floor of
three seeded workspaces) — it never touches a workspace that still holds a
pane, and never touches one that isn't last, so a middle workspace you're
mid-setup on is never at risk. ⌘N and the header strip's trailing `+` button
are the way back to a fourth workspace.

## Themes

Omarchy-style palettes, switched from the top-bar `<select>` (persists to
`localStorage`, applies app-wide including open terminals): `turing`
(default), `tokyo-night`, `gruvbox`, `catppuccin`, `nord`, `everforest`,
`kanagawa`, `rose-pine`, `matte-black`.

## Workspace presets (first launch only)

`defaultLayout()` only seeds a brand-new install — once anything is
persisted to a saved session these presets never apply again, and every pane
type below is always reachable via the ⌘p launcher regardless of what a
workspace starts with. Exactly three workspaces are seeded; there is no
seeded *empty* filler workspace anymore. Press ⌘N (or the header strip's
trailing `+`) for a fourth, up to a ceiling of nine — and ⌘W folds it away
again if you leave it empty.

| Workspace | Preset |
|---|---|
| ⌘1 | terminals — two shells, split |
| ⌘2 | results — metrics / images / flywheel |
| ⌘3 | agent debug — agent session list / cross-session feed |

## Panes

`term` (local shell via `portable-pty`), `queue` / `chat` / `obs` (the exact
browser-tab components, gateway-proxied), `metrics` (tails `metrics.jsonl` only —
`metrics.json` is a summary object with no series, and charting it produced an
empty pane on every completed round; multi-run overlay, one series at a time
via tabs, ETA strip naming the run it describes),
`images` (browses result images, see below), `flywheel` (parses
`<results>/loop-*/trajectory.json` round timelines), `agents` (live
Claude Code / Codex CLI session list + transcript tail), `agentfeed`
(cross-session tool/assistant activity feed).

### The `metrics` pane's verdict badge

Beside the run selector, one small badge per charted run, carrying that run's
own distinguishing path tail and saying whether the log under its curve
verifies. The pane cannot run the verifier — that is Python — so the loop runs
it at the end of every attempt and writes the answer to `metrics.verdict.json`
next to `metrics.json`; the badge reads that file. Each chip is a focusable
`<button>` with the qualifier attached as its accessible description, so the
sentence that keeps a green chip from reading as "these numbers are real" is
not reachable by mouse alone.

**The contract.** A run's chart is built only from bytes of **one file
generation**, applied in order: `fs_tail` reports the chunk's start offset and
the file's identity; the pane applies a chunk only if its start equals the
offset it holds *and* the identity matches — a changed identity or a shorter
file resets the run to that chunk alone; a chunk with any other start is
discarded, never appended. A trailing partial line is not consumed. `verified`
means the verdict's `chain_head` equals the `_chain` digest of the **last
non-blank line the pane holds**, and any last non-blank line it cannot parse
makes the run `stale` — as does an appended chunk that holds only blank lines,
which the writer never emits and `integrity.py` treats as a malformed record.
Every state the badge shows is derived from files that are **currently beside
the run**, re-read whenever the run's own file changes.

Two limits on "one file generation", stated honestly. (a) Where the platform
reports no file identity (`dev`/`ino` are `null` — anything but unix),
identity-based reset is unavailable and detection is length-only: a re-drive
whose new file is at least as long as the held offset is not seen as a new
generation there. (b) An in-place rewrite of the **same** inode that ends
longer than the pane's offset (`cp other.jsonl metrics.jsonl`) is invisible to
both identity and length; it is caught only by the **seam check** — because a
held offset always sits just after a `\n`, the first non-blank line of an
appended chunk must parse as a JSON object, and if it does not the run is
forgotten and re-read from byte 0. That is a heuristic, not identity: a rewrite
whose lines happen to end at the same byte offsets as the old ones passes it,
and the badge then answers for the rewrite's last line over a chart that still
begins with the old file's points.

Each clause is load-bearing. A line count is a property two different logs can
share, and a re-drive (the metrics trio and the verdict move together into
`prior-N/`) puts a fresh, unrelated chain at the same path; binding to the
digest is what stops a chip going green over bytes nobody checked. The new
file is a new inode, and its bytes can be at least as long as the offset the
pane held (a same-length first line; a create and two appends coalesced under
the watcher's debounce) — which is why the identity, not only the length,
decides whether a chunk continues the run or replaces it, and why a chunk that
starts anywhere but at the held offset is dropped rather than guessed about.
`fs_tail` is issued at most once at a time per run (a request during a read
coalesces into one follow-up), so two events a few milliseconds apart — the
last append and the verdict, the ordinary end of every attempt — cannot
deliver the same bytes twice. And because a file renamed away produces no
watcher event for its old path, the pane re-reads the verdict on every change
to the run file too — otherwise the badge would sit green over a directory the
verdict has left. The reverse edge is covered symmetrically: a verdict event
re-tails the run, so a watcher event dropped by the 300 ms debounce cannot pin
a clean run amber forever.

`fs_tail` (`desktop/src-tauri/src/fsroots.rs`) returns
`{ data, offset, start, dev, ino, restarted }`: `data` is whole lines only —
bytes `[start, offset)` cut at the last `\n`, the fragment after it re-read
complete on the next call; `start` is where the read began (the caller's
offset, or `0` with `restarted: true` when the file was shorter than it);
`dev`/`ino` are the file's identity on unix and `null` elsewhere. Length and
identity are read by `fstat` on the handle the bytes are then read from — one
open, not a stat followed by an open — so a rotation between the two cannot
label a new file's bytes with the old file's identity. Every offset is computed
on the bytes in Rust; the pane does no byte arithmetic of its own.

| Badge | Meaning |
|---|---|
| `✓ chain ok` | the loop's check passed, and the line it ended on is the last line this pane has parsed |
| `≠ chain stale` | the pane's last non-blank line does not match the digest the loop last checked — the pane is behind the file, the directory was re-driven and this verdict describes a generation that is no longer here, or the last line does not parse at all (which `verify` would fail; `failed` is reserved for what the loop itself recorded) |
| `? chain incomplete` | intact chain, no summary beside it, or one a writer was still appending to when the loop looked |
| `✗ chain FAILED` | the log does not recompute, or its summary disagrees with it. Never softened by a later line landing |
| `· unverified` | no verdict file beside this run — nothing was checked here. Also what a run **still being written** shows, since the verdict is written only once the attempt ends |

Precedence, when more than one rule could apply: **failed > stale > incomplete
> verified**, with `unverified` standing outside the ladder for "there is no
verdict file to read at all". A failure is never softened into `stale` by a
later line landing, and `verified` is only ever reached by falling all the way
through.

Note what is *not* a badge state: "the run is still being written". A live
attempt has no verdict beside it, so it reads `unverified` — never `stale`.
`stale` always means the pane and the loop are looking at different bytes.

**A green badge is not a trust boundary, and its wording is deliberate.** It
says `chain ok`, never "verified" or "trusted", and its tooltip carries the
qualifier in full: *chain internally consistent as last checked by the loop —
not proof the numbers are authentic or meaningful; run `python -m
turing.research.loop.verify <dir>` for an independent check*. The chain is
written by the same account that could rewrite it, a self-consistent forgery
is undetectable by construction, and a wrong verifier's wrong numbers chain
perfectly — see `docs/research-agent.md`'s "Integrity, and its limits". The
badge exists so an operator reading a curve knows whether anyone checked, not
so a green chip can stand in for having checked.

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
collapsing them would let a missing measurement read as a real result. Each
status also has its own colour, not just its own glyph — the wheel below draws
wedges with no glyph to carry the difference.

Two of the gates the loop writes, `noise_floor_available` and
`eval_set_stable`, are the *cause* of a refusal rather than an independent
failure, so a round is not marked `✗` for a gate that its own refusal verdict
already explains. Running without a noise-floor report is a supported path;
it reads `?`, not `✗`. `lineage_recorded` has no corresponding refusal and
stays a real failure.

**Click a round to expand its `round-NN/round.json`** underneath it — the
artifact written beside the trajectory row, carrying what the row flattens
away. The pane shows the per-cell numbers the loop is steered by (score,
correctness pass rate, Δ, the noise floor Δ is measured against, gain in noise
units with the beats-the-floor call, cost per point), the engine identity
including the scaffold sha, the gates, the per-cell saturation verdicts with
their reasons, and the lineage. The record's `problems[]` and the raw
`noise_floors[]` samples are not rendered — read the file itself for those. Lineage includes an explicit
**comparable-to-parent** line: rounds measured on different eval sets may not
be compared at all, so the pane says so rather than showing a delta that means
nothing. `[open round dir]` shells the directory out. A round still in flight
has no `round.json` yet and says so instead of blanking.

**`[wheel]`** swaps the timeline for a ring — one wedge per round, clockwise
from 12 o'clock, coloured by the same status glyphs, with the round count in
the hub. The loop visibly closes on itself and a run accumulating rounds reads
as momentum. Wedges are clickable, same detail as the list.

The timeline stays the default: it is denser and more scannable, which matters
most in the pane's usual size. The wheel drops its per-wedge numbers once
wedges get too thin for them and keeps its shape — the round count in the hub
still reads. How many rounds that takes depends on the pane's short side:
roughly 25 rounds at 200px, 37 at 300px, 50 at 400px. Below about 80px it
declines to draw at all rather than render a smudge, and `[wheel]` takes you
back to the list.

Note the wheel divides a full turn between the rounds, so a round landing
mid-run re-partitions the whole ring rather than extending it into spare
space. Nothing animates.

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

### The flywheel's `[metrics]` link, and how it coexists with `.viewer.json`

The flywheel pane can point the metrics pane too. Clicking a round expands
its detail in place, exactly as before; the expanded detail carries a
`[metrics]` link (next to `[open round dir]`, and also on a round whose
`round.json` hasn't landed yet — that's the round most worth watching live).
Clicking it selects **every live run of that round** in the metrics pane:
each `<loop>/round-NN/attempts/<problem-id>/metrics.jsonl` on disk, nested
problem ids included, excluding rotated `prior-N/` chains and dot-named
directories — at least as strict as the exclusions the loop makes when it
emits `.viewer.json` (the app additionally excludes *any* dot-named final
directory, not just the loop's `.rotating` staging name, and rejects a run
file sitting directly under `attempts/` with no problem-id segment), because
a superseded generation is not a run of the round. The selection is taken
when the click is honoured — attempts that appear later are picked up by
re-clicking the round. The active series tab is left alone: the link chooses
*which runs*, and your series choice keeps meaning what it meant.

The click acknowledges itself right beside the link, briefly: `→ metrics`
when a mounted metrics pane received it, `no metrics pane` when none was
mounted anywhere to hear it. The two outcomes are deliberately not the same
silence — a click that landed nowhere must not look like success.

If the round has no metrics on this machine (yet), nothing changes — the
metrics pane keeps its current chart, and the request is honoured later if
that round's first `metrics.jsonl` appears (the pane re-walks the results
root when the watcher reports a run file it has never listed, so a live
round's very first solver step is enough). No blank panes either way.

**Coexistence rule: last action wins, and the app never writes
`.viewer.json`.** The file is the agent → app direction only; if the app
wrote it to express a click it would stomp what an agent said and feed its
own watcher. So the click travels as an in-app event, and the metrics pane
holds exactly one pending request, whichever source it came from: a
`[metrics]` click replaces what `.viewer.json` last asked for, the file's
next change replaces the click, and a second click replaces that again.
Picking runs by hand in the metrics pane's own listbox is a later action
too: it clears whatever request was still pending, so a round clicked
earlier can never rearrange a chart you have since chosen yourself. This
holds under latency as well — a click that lands while a slow `.viewer.json`
read is still in flight survives that read resolving. An agent that never
competes with a click sees exactly the behaviour described above, unchanged.

## Agent-driven pane control

The same idea one level up: `.layout.json`, also in the results root, says
**which panes exist, where, and on which workspace**. The running app
rearranges itself on save — no relaunch, and no hand-editing the persisted
layout blob in the WebKit localStorage store, which is how this had to be
done before.

```json
{
  "workspaces": {
    "4": {
      "dir": "h",
      "ratio": 0.65,
      "panes": [{ "pane": "queue" }, { "pane": "chat" }]
    },
    "5": null
  },
  "active": 4
}
```

That is the motivating case in one file: set up a fourth workspace beyond
the seeded three, empty a fifth, and switch to the fourth. (Workspace 3 is
already the agent-debug view by default now — the compiler is driven
entirely by how many workspaces currently exist, not by a fixed count of
five. Unlike ⌘N, a request cannot grow that count itself — see below — so
this example presumes at least five workspaces already exist, created by
pressing ⌘N twice or by an earlier request.)

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

**Workspace keys are the numbers you press `⌘` with, `"1"` through `"9"`** —
the same numbers as the workspace table above, not 0-based indices. A key
naming a workspace beyond however many currently exist rejects the whole
request; create it first with ⌘N (or a prior request) if you need it.

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
