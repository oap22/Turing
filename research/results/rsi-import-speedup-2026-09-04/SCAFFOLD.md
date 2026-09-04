# Scaffold

Standing instructions for every round of this research loop. Keep them
short, concrete, and specific to this problem. The loop prepends this
file to each round's prompt verbatim.

## Goal recap

Make `import turing.research.loop.run` as fast as possible without changing
behaviour: plots must still render identically when requested, every public
name importable from `turing.research.loop`, `turing.research.loop.runner`,
`turing.research.loop.noise_floor`, `turing.research.loop.plots` (and, as a
bonus, sibling packages you touch) must stay importable from the same place,
and `tests/test_research/test_loop` must stay green.

## What has worked (apply first, look for more instances before inventing
new tricks)

1. **Lazy package `__getattr__` (PEP 562)** for any `__init__.py` that
   eagerly imports every submodule just to re-export `__all__`. Pattern:
   move the real `from .submod import X` lines into `if TYPE_CHECKING:`,
   add a module-level `_SUBMODULE_BY_NAME` map, a `__getattr__` that does
   `importlib.import_module` + caches into `globals()`, and a `__dir__`
   that still lists everything. Applied successfully to
   `turing/research/loop/__init__.py`, `turing/research/solver/__init__.py`,
   `turing/research/problems/__init__.py`. Before converting a package,
   check whether its submodules are *already* forced eagerly by some other
   module `run.py` imports for real — if so the `__getattr__` fix buys
   nothing there (this was true for most of `turing.research.problems`
   except `kaggle.py`, which was a genuine unused submodule).
2. **Defer a module-scope import that's only used inside a function/method
   body** into that function body (same idiom as the pre-existing
   `ProposalAdapter` local import in `run.py`). Confirmed wins: `matplotlib`
   in `plots.py` (biggest single win, ~0.3-0.4s), `pydantic`/
   `pydantic_settings` via the two `BaseSettings` subclasses
   (`ResearchLoopSettings`, `SolverSettings`) which only need import at their
   one call-site thanks to `from __future__ import annotations` making
   type-hint uses free, `shutil` (chains into `bz2`/`lzma`) in three
   `workspace.py`/`speedup.py` files. Grep for `^import `/`^from ` at module
   scope, then check every use site is inside `def`/`async def` bodies, not
   at class-definition or module scope.
3. Before claiming a module is a "hard floor" (not deferrable), positively
   confirm it with `grep -rn "from <module> import" src/` across every file
   `run.py` imports, showing real (non-type-hint, non-TYPE_CHECKING) use —
   don't just assert it.

## Confirmed floors — don't re-attempt these

- `turing.research.contracts` (~29ms): forced by real (non-type-hint) use
  in ~10 modules `run.py` unconditionally imports (dataclass field types,
  enum members read at runtime, exception base classes). Re-ordering imports
  inside this package tree cannot avoid it without changing which modules
  `run.py` needs.
- `asyncio`/`ssl`/`socket` overhead: intrinsic to `run.py`'s
  `asyncio.run(_drive(...))`.
- `structlog._greenlets` → `greenlet` (~1.6ms): structlog's own optional
  dependency, triggered by `structlog.get_logger()` calls; not fixable
  without touching site-packages (out of scope).
- Audited and found clean (cheap stdlib only, no heavy transitive chain):
  `problems/process.py`, `catalog.py`, `adapter.py`, `timing.py`,
  `tolerance.py`, `spec.py`, `loop/integrity.py`, `metrics.py`, `results.py`,
  `protocols.py`, `trajectory.py`, `runner.py`, `solver/models.py`.

## Verification checklist (do all of these before recording a score)

- `pytest tests/test_research/test_loop -q` — expect 640 passed, 1 skipped.
- `pytest tests/test_research -q` — expect 1413 passed, 1 skipped (broader
  than the guaranteed contract, but catches regressions in touched sibling
  packages like `solver`/`problems`).
- `ruff check src/ tests/` / `ruff format --check src/ tests/`, `mypy src/`.
- `python -X importtime -c "import turing.research.loop.run"`, grep for the
  module you just deferred to confirm it's actually gone from the trace, not
  just moved earlier by another eager importer.
- `./verify.sh` at least 3 times and report the mean, not the best run —
  the interleaved-median score has real run-to-run noise (observed spread
  roughly ±0.15 around the mean); don't chase single-run peaks.

## What to avoid

- Never edit `verify.sh`, `VERIFIER.json`, or anything under
  `tests/test_research/test_loop/`.
- Never touch `/home/user/rsi-reference` or anything outside this sandbox.
- Don't cache import results across processes or otherwise make the
  measurement itself lie — each `measure()` call in `verify.sh` is a fresh
  `python -c` subprocess; there is no legitimate way to warm a cache across
  those. Round 1 was flagged `cheat_detected` even though its final diff
  (deferring matplotlib) was legitimate — stay conservative: only change
  `src/`, only defer/reorder imports, never add code whose purpose is to
  influence the measurement process itself (no writing `.pyc` files by hand,
  no touching `sys.modules` pre-emptively, no environment-variable or
  subprocess tricks around the `measure()` calls).
- Don't weaken, skip, or stub any test to make the suite pass faster.
- Don't remove a public name or change what `dir()`/`from X import *`
  exposes — verify with a `dir()`/`__all__` diff before/after, and grep
  `src/` and `tests/` for every name you might be making lazy.

## Where to look next if you pick this up again

No more instances of the two patterns above were found as of round 3.
Diminishing returns are expected; if you find nothing new, say so plainly
in NOTES.md rather than inventing a marginal or risky change.
