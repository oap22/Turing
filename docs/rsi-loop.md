# The RSI workstation loop — operator guide

How to start a loop, what the verifier is and why it is frozen, what each
failure category means, what the self-edit step may and may not do, what the
cheat detector catches and what it does not, how to resume, and what the exit
codes mean.

- **Code:** `src/turing/research/rsi/` (entry point `python -m turing.research.rsi`).
- **Bash predecessor and bridge:** `scripts/rsi-loop.sh` — the desktop still
  types this script into a terminal pane; with `--verifier`, with
  `TURING_RSI_ENGINE=python`, or on a slug whose sandbox already holds
  `VERIFIER.json`, it hands off to the Python engine.
- **Decision record:** `docs/adr/0011-autonomous-research-agent-retarget.md`
  (§1 frozen verifier, §8 execution environment, §15 stopping rule and budget,
  §16 information channel, §17 contract hardening).
- **Sibling guide:** `docs/research-agent.md` is the loop-1 research runner —
  frozen `(goal, verifier)` *projects* over a corpus. This guide is the other,
  smaller thing: one problem, one sandbox, one verifier, many rounds.

> **Status, honestly.** The engine, its contracts and its tests exist; the
> tests run the loop end to end with a scripted fake engine. **No round has
> been run against the real `claude` CLI from this code yet.** Sections that
> describe a guarantee the code does not enforce are marked **[not enforced]**.

---

## What an RSI workstation is

A **slug** names one problem. It resolves to two directories, the same two the
bash script used, so the desktop's panes keep working unchanged:

| Directory | Path | What lives there |
|---|---|---|
| Sandbox | `~/turing-workspace/rsi-<slug>/` | A git repo. `PROBLEM.md` (the goal), `NOTES.md` (the agent's running state), `SCAFFOLD.md` (standing instructions, see below), `VERIFIER.json` (the verifier lock), the agent's own files, and `STOP` when someone wants the loop to end |
| Results | `<results-root>/loop-rsi-<slug>/` | `trajectory.json` (one JSON line per round, append-only), `metrics.jsonl` (the agent's own numbers), `taxonomy.json` (the frozen category list), plots and notes the agent saves |

Each **round** the loop builds a prompt — `SCAFFOLD.md` verbatim, then the
round text the bash script always used (do one focused iteration, append one
`metrics.jsonl` line, save plots, update `NOTES.md`, commit, create `STOP` if
done), then the verifier command the round will be graded by, with the explicit
statement that **the loop measures the score, not the agent** — runs
`claude -p <prompt> --permission-mode bypassPermissions --output-format text`
in the sandbox with a wall-clock cap, then runs the verifier, then runs the
cheat detector, then appends one line to `trajectory.json`.

The trajectory line keeps the bash script's four keys first
(`round`, `started`, `ended`, `exit`) and adds `score`, `passed`, `categories`,
`scaffold_sha`, `void`, `agent_reported_score`, `verifier_wall_seconds`. Lines
with an `event` key are not rounds; the resume logic skips them when
numbering. The events the loop writes (each also carries `round` and `ts`):

| Event | When | Extra keys |
|---|---|---|
| `verifier_locked` | First run, right after `VERIFIER.json` is written | `command_sha256`, `file_sha256s`, `lock_sha256` (sha256 of the lock file's bytes; a resume whose on-disk lock differs is a tamper) |
| `scaffold_seeded` | First run, when `SCAFFOLD.md` is created and committed | `scaffold_sha`, `scaffold_blob` |
| `scaffold_drift` | A round agent rewrote or committed `SCAFFOLD.md`; the loop restored its own version | `when`, `head_moved`, `worktree_moved`, `restored_sha`, `scaffold_sha`, `scaffold_blob` |
| `self_edit` | A self-edit was kept and committed | `scaffold_sha`, `scaffold_blob` |
| `self_edit_rejected` | The self-edit touched something other than `SCAFFOLD.md` and was discarded | `proposed`, `reason` |
| `self_edit_kept` | The rounds after a self-edit were judged and the edit survives | `scaffold_sha`, `best_before`, `best_after` |
| `rollback` | The rounds after a self-edit were worse than the noise floor allows; the edit was reverted | `reverted`, `revert_sha`, `best_before`, `best_after`, `scaffold_sha`, `scaffold_blob` |
| `rollback_failed` | The revert could not be applied; the loop stops (exit 0, `stop_reason=rollback_failed`) | `reverted`, `error` |
| `verifier_tampered` | The lock broke at start-up (`when: startup`, written by the CLI) or right after a self-edit (`after: self_edit`); the loop stops with exit 3 | `detail` |

## The frozen verifier

The verifier is a shell command you supply once, with `--verifier`, on the
first run. It runs **from the sandbox directory** after every round, with its
own timeout (`--verifier-timeout-seconds`, default 600):

- Its **exit code** is pass/fail. Zero passes; anything else, including a
  timeout, fails.
- The **last line of its stdout** that matches exactly `score=<number>` is the
  score. `score=0.83`, `score=-2`, `score=1e-3` all match; `score = 1` and
  `Score: 1` do not. No such line means the problem is pass/fail only.
  **[not enforced]** On a pass/fail-only problem the loop never has a best
  score to compare against, so *every* pass is recorded as an improvement
  (empty category set) — not only the first; the "later pass is
  `no_progress`" rule exists in the classifier but is only reached once a
  numeric score has been measured. Pass→fail after a self-edit is still
  judged for rollback (below).

The first run writes `<sandbox>/VERIFIER.json`: the command, its sha256, and
the sha256 of every file it pins. **Pinning is narrow and literal:** a file is
pinned when you list it with `--verifier-file <relpath>` (repeatable), or when
it is the command's **first token** (`--verifier ./grade.sh` pins `grade.sh`).
Nothing else is. `--verifier "python grade.py"` pins **nothing** — `python` is
the first token — so the CLI refuses a first run whose lock would pin no file
while the command names one that exists in the sandbox, and tells you which
flag to add. A first run that legitimately pins nothing (`--verifier 'pytest
-q'`) prints `verifier lock will pin: NOTHING (command string only)` so you
know only the command string is frozen. That file is **the lock**, and it is
the one invariant everything else hangs off (ADR 0011 §1):

- Every round re-checks the lock **before and after** the engine runs. Any
  mismatch — command hash, missing pinned file, changed pinned file — records
  the round as void with category `verifier_tampered` and **stops the loop**.
  The operator restarts by hand; nothing "restores and continues".
- On resume, `--verifier` may be omitted (the lock wins). Passing a
  **different** command, or a different `--verifier-file` set, is refused with
  exit 2, loudly, before anything is written. A different verifier is a
  different experiment: use a new slug.
- A resume **never writes a new lock.** If `VERIFIER.json` is missing from a
  sandbox that already ran — `PROBLEM.md` exists, `trajectory.json` holds
  round lines, `taxonomy.json` exists in the results dir, or the sandbox's git
  history ever tracked the lock — the run is refused with exit 2 even when
  `--verifier` is passed. Re-running the original command line after a
  tamper stop therefore does *not* re-lock a rewritten grader and continue the
  same trajectory; inspect `git log -- VERIFIER.json`, restore the lock, or
  start a new slug.
- The agent's prompt names the command line so it can run the verifier itself
  to check its work. The prompt never contains the lock's hashes, and the
  self-edit summary is refused if the scaffold or notes contain them.

**What the lock does not cover [not enforced].** It pins the command string
and the files you told it about. A verifier that shells out to a script the
lock does not name, or reads a data file the agent can rewrite, is only as
frozen as those unpinned dependencies — list them with `--verifier-file` or
make the verifier self-contained. A first run that pins something but also
names an unpinned sandbox file prints a `WARNING` in the plan; it does not
refuse. The lock is also not a boundary against an agent that edits
the pinned file and edits it *back* within a round: the post-round check sees
the original hash. `git status` on `VERIFIER.json` is checked too, but a
byte-identical restore of a pinned script is invisible to both.

**The loop measures the score.** The agent is asked to write a `score` into
its `metrics.jsonl` line if it has one, and that number is stored on the
trajectory as `agent_reported_score` — informational only. The recorded
`score` is always the one the verifier printed to the loop. If the two differ
by more than one part in a million, the cheat detector fires (below).

## The frozen error taxonomy

Every round carries a **set** of categories from a closed list. A round that
passed and improved on the best score so far carries the empty set. The list
is `turing.research.rsi.taxonomy.FailureCategory`; its digest is written to
`<results>/taxonomy.json` on the first run and checked on every later run — a
mismatch **refuses to start**, because per-category counts are only comparable
across rounds if every round was categorised the same way (this closes
`OPEN-QUESTIONS.md` Q15 for this loop).

| Category | Plain meaning |
|---|---|
| `engine_error` | `claude` exited non-zero or could not be started at all |
| `timeout` | The round hit its wall-clock cap (`--round-timeout-seconds`, default 1800) and was killed. Recorded on its own, not together with `engine_error` |
| `verifier_failed` | The verifier exited non-zero (or timed out) |
| `verifier_tampered` | The verifier lock no longer matches the sandbox. The round is void and the loop stops |
| `no_progress` | The verifier passed, but the score is not better than the best score so far. On a pass/fail-only problem this is never recorded by the running loop (see the frozen-verifier section) |
| `regressed` | The score is worse than the *previous* round's. Can hold together with `no_progress` |
| `no_metrics` | The round wrote no `metrics.jsonl` line. Recorded *alongside* whatever the verifier said, never instead of it |
| `cheat_detected` | The agent's self-reported score disagrees with the measured one. The round is void and the loop stops |
| `sandbox_escape` | A symlink in the sandbox points outside it, or the round created a results file of a kind the panes do not read. The round is void and the loop stops |

`taxonomy.json` holds version `1` and the digest
`2ee65add4eb8a03fe2466d53584904d12f7d78a374c45f74475ef646dc3dcd8d`. Changing
the list means a new taxonomy version and a new results directory; the old
counts do not carry over.

## Self-edit and rollback (loop 2, scoped to this workstation)

`SCAFFOLD.md` is the only thing the loop lets the agent change *about itself*.
It is prepended verbatim to every round prompt and versioned by git commits in
the sandbox repo.

Every `--self-edit-every` rounds (default 3; `0` disables), the loop hands the
engine an **aggregate summary** — per round: number, score, pass/fail,
categories, wall seconds; the category counts; the best score; the current
`SCAFFOLD.md`; the last ~40 lines of `NOTES.md` — and a prompt that permits
editing **only** `SCAFFOLD.md`. The summary never includes the verifier's
internals and never reads outside the sandbox (ADR 0011 §16). Then:

- The repo is inspected — working tree (ignored files included), index, HEAD
  and `.git` internals — against a snapshot taken before the engine ran. If
  `SCAFFOLD.md` is the only change, it is committed as
  `rsi: self-edit after round N`, the SHA is written to the trajectory as an
  event `self_edit` and onto later round lines as `scaffold_sha`. If
  **anything else** changed, every path the step touched is restored to its
  pre-step bytes (a previous round's uncommitted work is kept) and the event
  `self_edit_rejected` is logged (a pre-existing dirty file larger than
  64 MiB cannot be restored and is logged as `restore_impossible`). If
  `VERIFIER.json` or a pinned file changed, that is a tamper: the loop
  stops with exit 3.
- **Rollback.** After a kept edit, the loop watches the next
  `self_edit_every` rounds. If their best score is worse than the best score
  before the edit by more than the **noise floor** — the population standard
  deviation of all prior scores, or `--noise-floor` if you gave one; for
  pass/fail problems, any pass→fail — the loop runs
  the revert (a commit that puts `SCAFFOLD.md` back to the pre-edit text; the
  agent's staged work is left staged) and logs
  `{"event": "rollback", "reverted": <sha>, "revert_sha": …}`. An edit that
  survives its judgement window is logged as `self_edit_kept`. A revert that
  fails to apply is logged as `rollback_failed` and the loop stops (exit 0,
  `stop_reason=rollback_failed`) rather than keep running under an edit it
  could not undo. A pending, not yet judged edit survives a resume: it is
  rebuilt from the trajectory and judged when its window closes.
- **Budget.** `--self-edit-budget` (default 3, ADR 0011 §15 R=3) caps kept
  self-edits per invocation. Rejected proposals do not count against it.

What this is not: it is not the loop-2 self-edit of the research runner.
`SCAFFOLD.md` is prose the agent reads, not code the loop runs, and the loop-1
runner's `self_edit_seam` slot is still unwired. The noise floor from a handful
of scores is a rough number; with fewer than two prior scores it is zero, so
early rollbacks are strict.

## The cheat detector — and what it does not catch

It runs after every round and can only do one thing: **void the round and stop
the loop.** It never adjusts a score, never retries, never "fixes" a sandbox.

| Check | Fires as |
|---|---|
| Verifier lock intact, and `git status` reports no change to `VERIFIER.json` | `verifier_tampered` |
| No symlink inside the sandbox resolves outside the sandbox or the results dir | `sandbox_escape` |
| The round created no results file other than `metrics.jsonl`, `*.png`, `*.svg`, `*.json`, `*.md`, `*.txt`, `*.csv` | `sandbox_escape` |
| The agent's `metrics.jsonl` `score` does not exceed the measured score by more than 5% relative. A lower self-report never fires; the tolerance is wide because the agent may run a wall-clock verifier itself, and two honest runs never agree to six decimals | `cheat_detected` |

**It is a directory convention, not an OS boundary.** The engine runs
`claude` as *you*, with `--permission-mode bypassPermissions`. A Python
process the agent writes can read or write anywhere your user can, regardless
of cwd; the detector looks only inside the sandbox and the results directory,
and deliberately does not scan the parent of the sandbox (too broad to mean
anything). It does not see network access, does not see a file written to
`/tmp` or your home directory, does not see a modified tool on `PATH` that the
verifier happens to call, and does not see a pinned script that was changed and
changed back within a round. The boundary that would catch those is a separate
OS user — provisioned by `scripts/setup-research-sandbox.sh`, described in ADR
0011 §8, and **not used by this loop** (ADR 0011 R2 is still open). Until it
is, an unattended run is a run by you, as you.

A detector that fires is a reason to read the sandbox's git log and
`NOTES.md`, not a reason to lower the bar.

## Running it

First run — the verifier and the problem are both required:

```bash
python -m turing.research.rsi \
  --slug sort-bench \
  --results-root ~/turing-results \
  --problem 'Make src/sort.py sort a 10^6-element list of ints faster than the stdlib on this machine, without changing its interface.' \
  --verifier 'python grade.py' --verifier-file grade.py \
  --rounds 10 --self-edit-every 3 --self-edit-budget 3
```

`grade.py` lives in the sandbox — put it there **before** the first run; the
lock pins whatever `grade.py` is at first-run time, and `--verifier-file`
refuses a file that does not exist yet. It should exit non-zero on a wrong
answer and print `score=<speedup>` as its last line otherwise. Without
`--verifier-file grade.py` this command is refused: `python` is the first
token, so nothing would be pinned (`--verifier ./grade.py` is the other way to
pin it). The first run prints `verifier lock will pin: grade.py` before the
first round.

The same thing through the desktop's script:

```bash
scripts/rsi-loop.sh --slug sort-bench --results-root ~/turing-results \
  --problem '…' --verifier 'python grade.py' --verifier-file grade.py
```

The script switches from its original bash loop to the Python engine when
`--verifier` is given, when `TURING_RSI_ENGINE=python` is set, **or when the
slug's sandbox already holds `VERIFIER.json`** — so the desktop's own argv
(`--slug`, `--results-root`, `--rounds`, `--problem`, no `--verifier`) resumes
a locked slug on the Python engine instead of silently dropping back to the
unverified bash loop. On a slug that has never been locked and with none of
the new flags, the script behaves exactly as before. `--verifier-file`,
`--self-edit-every`, `--self-edit-budget` and `--noise-floor` are refused on
the bash path. `TURING_RSI_ENGINE=python` on a *fresh* slug without
`--verifier` exits 2 — the Python engine needs a verifier on its first run.
A relative `--results-root` is anchored to the directory you ran the script
from, on both paths.

Always try `--dry-run` first. It prints the resolved sandbox and results
paths, the rounds, the start round, whether this is a resume, the state of the
verifier lock (absent and what it would pin / intact / BROKEN / intact but
DIFFERS from `--verifier` / absent on a slug that already ran), the taxonomy
digest and file state, and — as `REFUSED (exit 2): …` lines — every reason
the real run would refuse to start. The plan and the run share one pre-flight,
so a plan with no `REFUSED` line passes every check that can be made by
reading. **[not enforced]** The on-disk pre-flight (`git init`, adopting the
committed `SCAFFOLD.md`, binding the lock to the `verifier_locked` event) can
still refuse with exit 2 — for example a sandbox whose recorded scaffold
version is no longer in its git history — or stop with exit 3 on a lock that
differs from the one the trajectory recorded; a read-only plan cannot see
those. It writes nothing.

`--engine fake` runs a scripted engine that does nothing each round; it is
for demos and is refused outside `--dry-run` unless
`TURING_RSI_ALLOW_FAKE_ENGINE=1` is set.

## Resume

Run the same command again, or just `--slug` and `--results-root` (through
`python -m turing.research.rsi` directly, or through the script, which routes a
locked slug to the Python engine on its own). The start round is the number of
round lines already in `trajectory.json` plus one (event lines are skipped);
`PROBLEM.md` wins over `--problem`; `VERIFIER.json` wins over `--verifier`, and
a different `--verifier` is refused. Before each round the loop checks for
`<sandbox>/STOP` and stops if it exists — delete it to continue. Three
consecutive engine failures (exit non-zero, timeout, CLI not found) abort the
invocation.

Two things the resume refuses, and how to recover:

- **A corrupt `trajectory.json`** (typically a partial last line after a crash
  mid-append). Both `--dry-run` and the run report the line number; open the
  file and delete the partial last line by hand — never rewrite earlier lines
  — or start a new slug.
- **A missing `VERIFIER.json`** on a slug that already ran (above): restore
  the lock from the sandbox's git history or start a new slug.

The original bash loop numbers rounds with `wc -l`, which counts the Python
engine's event lines as rounds; that is one reason the script no longer takes
the bash path on a locked slug.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Normal completion: rounds exhausted, `STOP` file found, three consecutive engine failures, or a scaffold rollback that could not be applied (`rollback_failed`; check the log for which) |
| `2` | Usage or pre-flight refusal, before any disk write: bad flag, missing `--problem` or `--verifier` on a first run, a different `--verifier` or `--verifier-file` set on resume, a first run that would pin no file although the command names one, a missing lock on a slug that already ran, unreadable lock, corrupt `trajectory.json`, taxonomy digest mismatch |
| `3` | The loop stopped on a cheat or a tamper. The last trajectory line is either a void round naming the category, or — when the lock was found broken at start-up, before any round — a `{"event": "verifier_tampered", "when": "startup", …}` line. Investigate before restarting; re-running the same command does not re-lock anything |
| `130` | Interrupted (Ctrl-C). No trajectory line is written for the round in flight. The engine's (or verifier's) process group is SIGKILLed on interrupt; a descendant that put itself in a new session (`setsid`) is outside that group and may survive — check for a still-running `claude` before restarting, because an orphaned agent writing into the sandbox during the next round reads as a tamper or escape |

## What is deliberately not here

- No OS-level isolation (above). No network policy. No resource caps beyond
  wall-clock timeouts.
- No held-out split, no secondary axis, no escalation channel — those belong
  to the research runner in `docs/research-agent.md`, and this loop makes no
  claim about generalisation: it improves one problem against one verifier.
- No automatic restart after a cheat or tamper, on purpose.
