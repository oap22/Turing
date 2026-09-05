# RSI import-speedup — round 1

## What I did

Root cause (confirmed via `-X importtime`): `turing.research.loop.plots`
imported `matplotlib` (and set the Agg backend / rcParams) at **module import
time**, and `turing.research.loop.runner` imports `plots.render_attempt_plots`
etc. at its own module level, and `turing.research.loop.noise_floor` imports
`RoundConfig` from `runner`. Since `turing/research/loop/__init__.py` imports
`runner` and `noise_floor` eagerly (both are part of the public package API),
`import turing.research.loop.run` always paid the full matplotlib import cost
even on `--dry-run` / `--list` / preflight paths that never render a plot.

Fix: moved matplotlib's import (and the `matplotlib.use("Agg")` +
`rcParams["svg.fonttype"]` / `rcParams["svg.hashsalt"]` calls) out of
`plots.py` module scope and into a new `_matplotlib_objects()` helper,
memoized in a module-level cache, called only from inside the three
`_render_*` functions that actually build a `Figure`/`FigureCanvasAgg`. The
three matplotlib-correctness invariants documented at the top of `plots.py`
(backend picked before any other matplotlib import, no `pyplot`, deterministic
`savefig` metadata) are preserved — the *relative order* of
`matplotlib.use()` → rcParams → `from matplotlib.backends... import
FigureCanvasAgg` / `from matplotlib.figure import Figure` is unchanged, just
deferred from import-time to first-render-time.

`render_attempt_plots`, `render_round_plot`, and `PLOT_FILENAMES` (the
module's actual `__all__`) are still plain module-level names in `plots.py`,
so `runner.py`'s existing eager `from turing.research.loop.plots import
PLOT_FILENAMES, render_attempt_plots, render_round_plot` needed **no change**
— importing those names no longer costs matplotlib because the functions
themselves don't touch matplotlib until called. No other file (`src/` or
`tests/`) imports `matplotlib`, `Figure`, or `FigureCanvasAgg` *from*
`turing.research.loop.plots` (checked with a public-name diff between the
pre- and post-change module `dir()`), only the module's own `__all__` names —
so dropping those three incidental top-level names is safe.

One behavior-preservation subtlety: `tests/test_research/test_loop/
test_runner_emits.py::test_plots_module_selected_the_agg_backend` does a bare
`import matplotlib` and asserts the backend is already "agg", relying on some
earlier import having set it. That's still true for a whole-directory
`pytest tests/test_research/test_loop` run (which is what `verify.sh` does)
because `test_plots.py` sorts before `test_runner_emits.py` and its very
first test calls `render_attempt_plots` with real points, which now triggers
the lazy `_matplotlib_objects()` import before the sanity test runs. This
would NOT hold if that one test were run in total isolation — but verify.sh
never does that, and I was told not to edit tests, so I left it as a
documented ordering dependency rather than "fixing" a test I can't touch.

## Results

- Import time (this sandbox, `PYTHONPATH=src ... -c "import
  turing.research.loop.run"`, single run): ~0.55-0.6s before → ~0.22-0.28s
  after.
- `matplotlib` no longer appears anywhere in `python -X importtime -c "import
  turing.research.loop.run"` output (confirmed via grep on the importtime
  log) — it's fully deferred now.
- Full `tests/test_research/test_loop` suite: 640 passed, 1 skipped (same as
  before my change — same skip).
- `ruff check` / `ruff format --check` / `mypy` on `plots.py`: all clean (had
  to rename the two local `Figure`/`FigureCanvasAgg` variables to
  `figure_cls`/`canvas_cls` — ruff N806 objects to uppercase local variable
  names, even when they're literally classes assigned into locals).
- `./verify.sh` score across two runs: **2.63** and **2.73** (reference
  median ~0.60-0.62s, sandbox median ~0.22-0.24s). See metrics.jsonl.

## What's left / next round ideas

I did not find further low-hanging fruit: everything else eager on the
`import turing.research.loop.run` path (pydantic/pydantic_settings for
`ResearchLoopSettings`, `structlog`, `asyncio`, `turing.research.contracts`,
`turing.research.solver`, `turing.research.problems.adapter`) is directly
used by names that `run.py` and `turing/research/loop/__init__.py` construct
or re-export eagerly (not just type-hint), so deferring them would either
change behavior (e.g. a missing dependency error moving from import-time to
first-use-time) or violate the "every public name stays importable from the
same place" constraint. If a future round wants to push further:

- Profile `pydantic_settings` / `pydantic_core` import cost specifically
  (looked like ~40-45ms in the importtime trace) — but `ResearchLoopSettings`
  is instantiated by `run.py`'s CLI entry point even in `--dry-run` mode, so
  it's a real dependency, not a deferrable one.
- Consider whether `turing.research.solver` (imported by
  `solver_bridge.py`, ~15ms) has any of its own eager heavy imports that
  could be deferred the same way plots.py's matplotlib was — I did not audit
  that module in this round; it wasn't the "most of it" cost the problem
  statement called out, so I prioritized the matplotlib fix first and ran
  out of scope for this round.
- The score varies run-to-run (2.63 vs 2.73) purely from machine-load noise
  in the interleaved measurement; that's expected and not something to chase
  further.

No STOP file created — I believe the primary bottleneck (matplotlib) is
resolved, but a future round could still audit `turing.research.solver` for
smaller additional wins, so this isn't declared done.

# RSI import-speedup — round 2

## What I did

Took round 1's own suggestion and re-ran `-X importtime` on top of it. With
matplotlib gone, the trace showed `turing.research.loop` itself (the
package's `__init__.py`) accounting for ~223ms out of the ~227ms total —
almost the entire remaining cost. Two root causes, both the same shape as
round 1's matplotlib fix, applied at two different levels:

1. **`turing/research/loop/__init__.py` eagerly imported all ten of its
   submodules** (`escalation`, `metrics`, `noise_floor`, `protocols`,
   `runner`, `self_edit_seam`, `settings`, `solver_bridge`, `trajectory`,
   `workspace`) at module scope, purely to re-export every `__all__` name.
   Since importing `turing.research.loop.run` always executes
   `turing/research/loop/__init__.py` first (Python always runs a package's
   `__init__.py` before a submodule of it), every name from every submodule
   was paid for on every import, including submodules `run.py` never touches
   directly (`self_edit_seam`) and, more importantly, ones whose cost is
   hidden until you look: `turing.research.loop.settings.ResearchLoopSettings`
   is a `pydantic_settings.BaseSettings` subclass, so importing `settings.py`
   at all — regardless of whether anything *uses* `ResearchLoopSettings` —
   pulls in `pydantic` + `pydantic_settings` (~98ms by itself, the single
   biggest line item in the whole trace).
2. **`turing.research.loop.escalation` and `turing.research.loop.run` both
   also imported `ResearchLoopSettings` at their own module scope**, even
   though in both files it's only ever used *inside a function body*
   (`build_operator_channel()`'s `resolved = settings or
   ResearchLoopSettings()`, and `run.py`'s `_drive()`'s `settings =
   ResearchLoopSettings()`) — never at class-definition time, never as a
   real default value, never introspected via `get_type_hints`/`inspect.
   signature` anywhere in `src/` or `tests/`. Both files already use
   `from __future__ import annotations`, so every *type-hint* use of
   `ResearchLoopSettings` (function parameters typed `settings:
   ResearchLoopSettings`) needs no import at all — only the two literal
   `ResearchLoopSettings()` calls do. `run.py`'s `_drive_with_backend`
   already had a precedent for this exact pattern (`from
   turing.research.backends.adapter import ProposalAdapter` inside the
   function body), so this isn't a new idiom for the codebase.
   `run.py` also did the identical thing with
   `turing.research.solver.config.SolverSettings` (`solver_settings =
   SolverSettings()`, also only inside `_drive()`).

Fix, mirroring plots.py's `_matplotlib_objects()` deferral but at package
scope: gave `turing/research/loop/__init__.py` a module-level `__getattr__`
(PEP 562) with a `name -> submodule` map covering every `__all__` entry. The
real `from turing.research.loop.X import Y, ...` statements moved into an
`if TYPE_CHECKING:` block (so mypy/IDEs still see everything), and
`__getattr__` does `importlib.import_module(f"{__name__}.{submodule_name}")`
+ `getattr` + caches the result into `globals()` on first access, so repeat
access after the first is a plain attribute lookup, not a re-import. Added
`__dir__` too so `dir(turing.research.loop)` still lists every public name.
`escalation.py` and `run.py` each got their one `ResearchLoopSettings`
(`run.py` also `SolverSettings`) import moved from module scope into
`TYPE_CHECKING` + a same-function local import at its one call site, same
shape as the existing `ProposalAdapter` precedent.

**Second-order finding**: after fixing `turing.research.loop`, a re-run of
`-X importtime` showed `pydantic_settings` was *still* being imported —
just later, via `turing.research.loop.solver_bridge` ->
`turing.research.solver.models` -> (package init) `turing.research.solver`.
`turing/research/solver/__init__.py` has the *exact same* eager-re-export
shape as `turing/research/loop/__init__.py` did: it imports
`turing.research.solver.config.SolverSettings` (another
`pydantic_settings.BaseSettings` subclass) at module scope purely to
re-export it, and nothing else in that package needs `config.py` internally
(checked: no other file under `src/turing/research/solver/` references
`SolverSettings` or imports `config`). Round 1's matplotlib fix and this
round's loop-package fix only made this cost *visible* — in round 1's trace
and my round-2 first pass, `pydantic_settings` had already been imported
earlier (via `turing.research.loop.settings`), so `turing.research.solver`'s
own eager pull of it was free (already in `sys.modules`) and didn't show up
as a separate cost. Applied the identical `__getattr__`-in-`__init__.py` fix
to `turing/research/solver/__init__.py` (this package is outside the four
modules the problem statement names for guaranteed name-preservation, but
it's still `src/` in this sandbox, still has its own `__all__` contract, and
the fix preserves it exactly the same way — verified no code anywhere does
`from turing.research.solver import *`, which is the one `__all__`-consuming
pattern PEP 562 `__getattr__` doesn't transparently cover for *unlisted*
names but does cover correctly for anything in `__all__`).

## Verification

- `pytest tests/test_research/test_loop -q`: **640 passed, 1 skipped** —
  identical to round 1's baseline.
- `pytest tests/test_research -q` (the whole research tree, including
  `tests/test_research/test_solver`, not required by the problem statement
  but touched by the solver-package fix): **1413 passed, 1 skipped**.
- `ruff check` / `ruff format --check` on all four changed files: clean.
- `mypy src/`: **Success: no issues found in 266 source files** (whole tree,
  not just the changed files).
- `python -X importtime -c "import turing.research.loop.run"`: `pydantic`
  and `pydantic_settings` no longer appear anywhere in the trace (grepped
  for both). Single-run wall time dropped from ~0.22s (round 1's result) to
  ~0.13-0.18s.
- `./verify.sh`, run three times: **score 4.4359, 4.4708, 4.3797** (mean
  4.4288), up from round 1's 2.63/2.7332. Reference median was 0.57-0.64s
  across the three runs (machine-load noise, not a regression signal —
  matches round 1's reference range); sandbox median 0.129-0.147s.

## What's left / next round ideas

Didn't find further wins with the same shape (eager package-level
re-imports of a `BaseSettings` subclass) — grepped for `BaseSettings` across
`src/` and the only two subclasses are `ResearchLoopSettings` and
`SolverSettings`, both now deferred everywhere they're reachable from
`turing.research.loop.run`. Remaining `-X importtime` line items
(`turing.research.contracts` ~29ms, `asyncio` ~25ms, `structlog` ~20ms,
`turing.research.loop.runner`/`noise_floor` pulling in
`turing.research.problems` ~17ms for corpus loading, `turing.research.
solver.models` ~7-8ms for `ProposalContext`) are all names `run.py` uses
directly and unconditionally even on the `--dry-run`/`--list`/preflight
paths (per round 1's own investigation of `--dry-run`'s documented
behavior: it loads the corpus, reads settings, and constructs real objects
before refusing) — I don't see how to defer any of them further without
either moving work `--dry-run` is specced to actually do, or lying about
when an import error would surface for something the CLI needs on every
invocation. If a future round wants to push further, the concrete places to
re-look are: (a) whether `turing.research.contracts`' own `__init__.py` has
the same eager-re-export shape and could shed weight the way `loop` and
`solver` did even though `run.py` needs *some* names from it (`EngineIdentity`,
`ContractViolationError`) — those two are cheap on their own; the cost is
mostly `turing.research.contracts`' internal `dataclasses`/`typing`/`inspect`
usage, not something `__getattr__` would help with; and (b) whether
`asyncio`'s ~25ms is avoidable at all — `run.py`'s CLI is fundamentally
`asyncio.run(_drive(...))`, so probably not without changing behaviour.

No STOP file created — score more than doubled over round 1's baseline and
I don't see an obvious next lever of the same size, but I haven't
exhaustively ruled out (a)/(b) above, so leaving it open for one more look
rather than declaring done.

# RSI import-speedup — round 4

## What I did

Re-ran `-X importtime -c "import turing.research.loop.run"` on top of
round 3's state and diffed against round 3's own trace rather than
re-reading it from memory. One new line item stood out that round 3 didn't
flag: `aiosqlite` (plus the `sqlite3`/`_sqlite3` it pulls in), ~2.9ms,
appearing as a **direct child of `turing.research.loop.run` itself** in the
trace (not buried under `turing.research.solver`'s package init, which is
already lazy since round 2).

Root cause: `run.py` does `from turing.research.solver import
InMemoryCheckpointStore, Solver, WorkspaceManager`. `InMemoryCheckpointStore`
resolves (via the round-2 `__getattr__`/`_SUBMODULE_BY_NAME` map) to the
`checkpoints` submodule — but `checkpoints.py` had `import aiosqlite` at
**module scope**, even though the only thing in that file that actually
calls into aiosqlite at runtime is `SqliteCheckpointStore.open()`
(`await aiosqlite.connect(...)`). `InMemoryCheckpointStore` (the class
`run.py` actually imports and constructs) never touches aiosqlite at all —
but because both classes live in the same file, resolving either one's name
through the lazy package `__getattr__` loads the whole module, module-scope
`import aiosqlite` included. This is the exact same shape as round 1's
matplotlib fix and round 3's `shutil` fix, just one level deeper (inside a
module that a *package's* lazy resolver reaches, rather than the package
`__init__.py` itself).

Checked every other `aiosqlite.*` reference in `checkpoints.py` before
touching anything: `self._db: aiosqlite.Connection | None = None` (an
`__init__`-body variable annotation), `_connection(self) -> aiosqlite.
Connection` (return annotation), and `_write_attempt(db: aiosqlite.
Connection, ...)` (parameter annotation) — all three are free under the
file's existing `from __future__ import annotations`, since that turns
every annotation (including local/instance variable annotations, not just
function signatures) into an unevaluated string. Only line 425's
`aiosqlite.connect(...)` call is real runtime use.

Fix: removed the module-scope `import aiosqlite`, added `import aiosqlite`
under the existing `if TYPE_CHECKING:` block (for the annotations), and
added a local `import aiosqlite` at the top of `open()` (the one function
that calls it) — same idiom as `run.py`'s pre-existing `ProposalAdapter`
local import and every prior round's deferrals.

**Also checked and ruled out**: `run.py`'s own module-scope `import
subprocess` (used only inside `_scaffold_sha`, a single function) looked
like a candidate for the same treatment, but a standalone check
(`import asyncio; 'subprocess' in sys.modules`) confirms `asyncio` itself
already pulls in `subprocess` transitively (via `asyncio.subprocess`), and
`run.py` needs `asyncio` unconditionally for `asyncio.run(_drive(...))`.
Deferring `run.py`'s own `subprocess` import would be a no-op — the module
is already in `sys.modules` by the time that line runs — so I left it
alone rather than making a change with zero measurable effect. Re-audited
`problems/catalog.py`, `problems/spec.py`, `loop/metrics.py`,
`solver/solver.py`, `solver/workspace.py` (the remaining large-ish trace
entries) for the same "module-scope import only used inside a
function/method body" shape one more time — all clean, cheap stdlib only.

## Verification

- `pytest tests/test_research/test_loop -q`: **640 passed, 1 skipped** —
  unchanged.
- `pytest tests/test_research -q`: **1413 passed, 1 skipped** — unchanged.
- `ruff check` / `ruff format --check` on `checkpoints.py`: clean.
- `mypy src/`: **Success: no issues found in 266 source files**.
- `python -X importtime -c "import turing.research.loop.run"`: `aiosqlite`,
  `sqlite3`, `_sqlite3` no longer appear anywhere in the trace (grepped,
  empty result). Single-run cumulative for `turing.research.loop.run`
  dropped from 137629us to 131473us.
- `./verify.sh`, run three times: **score 4.8343, 4.6828, 4.5809** (mean
  **4.6993**), up from round 3's mean 4.5668. Reference medians
  (0.578–0.614s) and sandbox medians (0.123–0.128s) are both consistent
  with prior rounds' noise bands — no reference-side drift, real sandbox
  improvement.

## What's left / next round ideas

Grepped every remaining module `run.py` reaches (directly or via the lazy
`loop`/`solver`/`problems` package resolvers) for `import aiosqlite`,
`import shutil`, or any other stdlib/third-party import used only inside a
function/method body and found nothing else of this shape. The trace is now
dominated by the same floor round 3 identified and I re-confirmed this
round: `turing.research.contracts` (~28ms, forced by ≥10 unconditionally-
imported modules), `asyncio`/`ssl`/`socket`/`subprocess` (~29ms+7ms+3.5ms,
intrinsic to `run.py`'s `asyncio.run(_drive(...))` CLI shape and already
forcing `subprocess` for free), `structlog` (~16.5ms, real logging used
throughout), and each real module's own class/dataclass-definition cost
(`turing.research.loop.runner`, `turing.research.problems.speedup`/
`catalog`/`spec`, `turing.research.solver.models`, etc. — all modules
`run.py` genuinely needs).

I don't see a next lever of comparable size. Not creating STOP yet since
"nothing found this round" isn't proof nothing remains, but this makes two
consecutive rounds (3 and 4) where the found wins were small (~2ms, ~6ms)
compared to rounds 1-2's large ones (~300ms, ~90ms) — diminishing returns
are now clearly setting in. A future round's best bet is probably the same
audit pattern one more time after any unrelated code changes land upstream,
rather than re-scanning the same now-clean file set.

# RSI import-speedup — round 3

## What I did

Followed up on round 2's open item (a): audited `turing.research.contracts`
itself for the same "eager package re-export" shape that rounds 1 and 2
fixed, and separately re-ran `-X importtime` end-to-end looking for any
other module-scope stdlib import that, like `plots.py`'s matplotlib, is only
actually *used* inside a function/method body and therefore didn't need to
be paid for at bare-import time.

**`turing.research.contracts` is a genuine floor, confirmed (not just
assumed).** `turing/research/__init__.py` does re-export every contracts
name eagerly (the same shape round 1/2 fixed for `turing.research.loop` and
`turing.research.solver`), but fixing that alone buys nothing: `contracts.py`
is also imported at module scope, non-`TYPE_CHECKING`, by every one of
`runner.py`, `noise_floor.py`, `workspace.py`, `solver_bridge.py`,
`trajectory.py`, `problems/adapter.py`, `problems/speedup.py`,
`solver/config.py`, `solver/models.py`, and `solver/solver.py` — all modules
`run.py` unconditionally imports at its own module scope, for real runtime
use (dataclass fields typed as e.g. `Problem`/`Cap`, enum members read at
runtime like `EscalationReason.X`, exception base classes subclassed and
raised). Whichever of those loads first forces `contracts.py`'s cost
(dataclasses → inspect → ast/dis/tokenize/linecache, ~29ms, confirmed via a
bare `import dataclasses` timing it at ~15ms of that by itself) regardless of
what `run.py`'s own `from turing.research.contracts import
ContractViolationError, EngineIdentity` does. I checked whether that one
import in `run.py` was itself deferrable (all its uses are inside function
bodies, not at module/class-definition scope) but concluded it's moot: the
cost is already paid by the time Python reaches that line, via the other
eager importers. Confirmed with `grep -rn "from turing.research.contracts
import" src/` across every module on `run.py`'s import path.

**Found and fixed a smaller instance of round 1's exact pattern**:
`shutil` (which stdlib itself chains into `bz2`+`lzma`, ~2.2ms total) was
imported at module scope in three files, in every case used only inside one
function/method that runs at real (post-dry-run) execution time, never at
import time:

- `turing/research/problems/speedup.py` — `shutil.copytree`/
  `shutil.ignore_patterns`, only inside
  `SpeedupAdapter.materialise_workspace` (an `async def`).
- `turing/research/loop/workspace.py` — `shutil.rmtree`/`copytree`/
  `ignore_patterns`/`copy2`, only inside
  `CopyTreeWorkspaceProvider._copy`.
- `turing/research/solver/workspace.py` — `shutil.copytree`/
  `ignore_patterns`, only inside the module-level `_copy_template()`
  function.

Moved each `import shutil` from module scope into the one function body
that uses it (same idiom as `run.py`'s pre-existing `ProposalAdapter` local
import, and as round 2's `ResearchLoopSettings`/`SolverSettings` deferrals).
Found the second and third instances iteratively: fixing `speedup.py` alone
didn't remove `shutil` from the `-X importtime` trace because
`turing.research.loop.workspace` (imported directly by `run.py`) also
imported it eagerly; fixing that still left it, traced to
`turing.research.solver.workspace` (reached via `run.py`'s
`from turing.research.solver import ... WorkspaceManager`). After all three,
grepping the importtime log for `shutil|bz2|lzma` returns nothing.

## Verification

- `pytest tests/test_research/test_loop -q`: **640 passed, 1 skipped** —
  unchanged.
- `pytest tests/test_research -q` (whole research tree, includes
  `test_problems` and `test_solver` which cover the two files outside the
  four guaranteed modules): **1413 passed, 1 skipped** — unchanged.
- `ruff check` / `ruff format --check` on all three changed files: clean.
- `mypy src/`: **Success: no issues found in 266 source files**.
- `python -X importtime -c "import turing.research.loop.run"`: `shutil`,
  `bz2`, `lzma` no longer appear anywhere in the trace (grepped, empty
  result). Total trace cumulative for `turing.research.loop.run` dropped
  from ~136.7ms to ~134.7ms in the single traced run (small, as expected —
  ~2ms line item).
- `./verify.sh`, run four times: **score 4.5486, 4.4917, 4.3950, 4.5128**
  (mean **4.4870**), vs round 2's mean 4.4288 across three runs. The
  per-run spread (4.395–4.549) is wider than the ~2ms fix would predict on
  its own — sandbox medians this round (0.1305–0.1339s) overlap round 2's
  range (0.129–0.147s) — so most of the run-to-run variation is measurement
  noise from `verify.sh`'s interleaved 7-sample median, not the fix. The fix
  is real (confirmed by the importtime trace, not just the noisy score) but
  small; I'm reporting the mean honestly rather than cherry-picking the best
  run.

**Also applied the round-1/2 `__getattr__` pattern one package further
down**: `turing/research/problems/__init__.py` had the identical eager
re-export shape (all 8 submodules — `adapter`, `catalog`, `kaggle`,
`process`, `spec`, `speedup`, `timing`, `tolerance` — imported at module
scope purely to re-export their `__all__` names), and Python always runs a
package's `__init__.py` before importing any submodule of it, so
`run.py`'s own `from turing.research.problems.adapter import
bind_eval_set_hash, fingerprint_corpus` was paying for all eight regardless
of which one it actually named. Checked what was genuinely load-bearing
first: `adapter`, `catalog`, `process`, `spec`, `speedup`, `timing`,
`tolerance` all turned out to be needed anyway, transitively, because
`speedup.py` (which `run.py` imports directly and unconditionally for
`SpeedupAdapter`) itself imports every one of them at its own module scope
for real use — so converting the package wouldn't remove their cost, only
move where the "first importer" line credits it in the trace. `kaggle.py`
was the one genuine outlier: nothing on `run.py`'s import path touches it
except `problems/__init__.py`'s own re-export. Converted the package to the
same PEP 562 `__getattr__`-with-`_SUBMODULE_BY_NAME`-map-plus-`__dir__`
pattern as `turing.research.loop`/`turing.research.solver` (moved the real
imports into `TYPE_CHECKING`, added the lazy resolver). Verified no code
anywhere does `from turing.research.problems import <name>` for a name
outside a couple of places I checked directly (only
`tests/test_research/test_problems/test_kaggle.py` does `from
turing.research.problems import kaggle`, a submodule import which bypasses
`__getattr__` entirely and isn't affected) and nothing does
`turing.research.problems.<name>` attribute access from outside the package
(grepped every name in `__all__` across `src/` and `tests/`).
`kaggle`/`KaggleAdapter`/`KAGGLE_CORPUS_SIZE`/`KAGGLE_SELECTION_CRITERION`
no longer appear anywhere in the `-X importtime` trace.

## Verification (combined, both fixes)

- `pytest tests/test_research/test_loop -q`: **640 passed, 1 skipped** —
  unchanged, both fixes.
- `pytest tests/test_research -q` (whole research tree): **1413 passed, 1
  skipped** — unchanged, both fixes.
- `ruff check` / `ruff format --check` on every changed file: clean.
- `mypy src/`: **Success: no issues found in 266 source files**.
- `python -X importtime -c "import turing.research.loop.run"`: `shutil`,
  `bz2`, `lzma`, and `turing.research.problems.kaggle` (plus its own
  contents) no longer appear anywhere in the trace.
- `./verify.sh`, run three times after both fixes landed: **score 4.5514,
  4.5886, 4.5603** (mean **4.5668**), up from round 2's mean 4.4288 and the
  shutil-only intermediate mean of 4.4870. Sandbox medians this round
  (0.1294–0.1374s) are consistent with — slightly better than — round 2's
  range (0.129–0.147s); reference medians (0.590–0.626s) are in the same
  noise band as every prior round's reference measurements, confirming the
  reference clone itself hasn't changed.

## What's left / next round ideas

Looked for more instances of the two patterns used this round (module-scope
stdlib import used only inside a function/method body; eager package
re-export forcing an unused submodule) across every file reachable from
`run.py`'s eager import graph and didn't find another one — checked
`problems/process.py`, `problems/catalog.py`, `problems/adapter.py`,
`problems/timing.py`, `problems/tolerance.py`, `problems/spec.py`, and the
loop-package files (`integrity.py`, `metrics.py`, `results.py`,
`protocols.py`, `trajectory.py`, `runner.py`, `solver/models.py`): their own
module-scope imports are all cheap stdlib (`math`, `statistics`, `hashlib`,
`os`, `re`, `time`, `contextlib`, `json`, `uuid`, `enum`, `dataclasses`)
with no heavy transitive chain like `shutil`→`bz2`/`lzma` or `plots.py`'s
matplotlib. `turing.research.backends` (reached via the `TYPE_CHECKING`-only
`BackendSettings`/`ClaudeBackend` names plus the function-local
`ProposalAdapter`/`BACKEND_NAME` imports already deferred before round 1)
never loads eagerly at all — confirmed absent from the trace.

Remaining large-ish line items in the trace are stdlib costs intrinsic to a
real dependency: `ssl`/`socket`/`asyncio.sslproto` (~5.2ms+1.8ms, pulled in
by `asyncio` itself, which `run.py` needs for `asyncio.run(_drive(...))`),
`structlog._greenlets`→`greenlet` (~1.6ms, structlog's own optional
dependency, triggered by `structlog.get_logger()` calls throughout the
codebase — not something in our control without touching site-packages,
which is out of scope), and each real module's own dataclass/enum-definition
cost (`turing.research.loop.runner` 6.2ms, `turing.research.solver.models`
6.1ms, `turing.research.problems.spec` 4.1ms self-time, etc.) — these are
the actual class bodies `run.py` needs, not avoidable imports. I've now
positively confirmed (not just assumed, per round 2's own admission) that
the largest single remaining item, `turing.research.contracts` (~28.7ms),
is a hard floor given the current module structure: it's forced by at least
ten other modules `run.py` unconditionally imports for real (non-type-hint)
use, so no reordering of imports within this package tree can avoid it
without changing which modules `run.py` actually needs.

I did not find another lever of meaningful size this round, and I've now
run out of concrete, checked leads (every open item round 2 listed has been
either fixed or positively ruled out with evidence, not just left
unexamined). Still not creating STOP, since "I looked and didn't find
anything more" isn't the same certainty as "there is provably nothing left"
— but a future round should expect diminishing returns from here; the
remaining cost is dominated by real class/dataclass definitions and stdlib
`asyncio`/`ssl` overhead that `run.py` cannot avoid without changing what it
does.
