"""The Kaggle family — an explicit, deliberate stub.

This module exists to hold a shape and a rule, not an implementation. The rule
is the more important half.

**The five competitions are not selected yet.** Until they are, there is
nothing to load, and a partial implementation would be worse than none: it
would invite the corpus to grow one competition at a time, and the corpus must
be **locked before the noise floor and unchangeable after**. Rounds measured on
different eval sets may not be compared at all, so "start with two and add
three later" does not build the benchmark up, it restarts the trajectory each
time.

**The selection criterion, pre-registered.** A competition enters the corpus
if and only if it is *mechanically feasible*:

    It completes end-to-end on this Mac in under N hours with the baseline
    solver.

That is the whole test. **Never expected score, never "looks promising", never
"the agent seemed to do well on this one".** If tasks enter the corpus because
the agent performs well on them, every downstream number is invalid and no
amount of later rigor recovers it — the corpus would have been selected by the
thing it is meant to measure. The criterion is written down here, before any
result exists, which is the only moment at which writing it down is worth
anything.

Feasibility is the criterion because the machine is the binding constraint:
MLE-bench assumes a CUDA box, and on an M4 Pro with Metal and no cluster,
tabular and classical-ML competitions run fine while serious vision and NLP
training tasks are impractical or impossible. Taking a published task list
unfiltered would load the corpus with tasks the machine cannot finish, which
score as failures and drag round 0 toward the "solve rate near zero" kill
condition for reasons that have nothing to do with the idea being tested. The
resulting deviation from the published list is a thing to state openly in the
writeup, not to hide.

**Why five and not one.** A type with one problem in it is not a measurement:
no trend is readable off a single task at any number of rounds, and a
demonstration sitting beside a measurement invites being reported as one.

When this is implemented it must satisfy
:class:`~turing.research.problems.adapter.ProblemAdapter` unchanged — the core
never learns that Kaggle exists. Its verifier reports a continuous score on the
``leaderboard_percentile`` scale, which is **never averaged against the speedup
family's ratios**.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from turing.research.contracts import ProblemType

if TYPE_CHECKING:
    from pathlib import Path

    from turing.research.contracts import Problem

__all__ = [
    "KAGGLE_CORPUS_SIZE",
    "KAGGLE_SELECTION_CRITERION",
    "KaggleAdapter",
]

#: The pre-registered, mechanical, feasibility-only selection criterion.
#: Recorded as a constant so it can be quoted in a round record and cannot be
#: quietly restated later.
KAGGLE_SELECTION_CRITERION = (
    "A competition enters the corpus if and only if it completes end-to-end on this "
    "Mac in under N hours with the baseline solver. Feasibility only — never expected "
    "score, never 'looks promising'. Tasks selected by how well the agent does on them "
    "invalidate every downstream number."
)

#: Experiment 1's Kaggle-family size. Deliberately light, but not one: a type
#: with a single problem in it is a demonstration, not a measurement.
KAGGLE_CORPUS_SIZE = 5

_NOT_SELECTED = (
    "The five Kaggle competitions have not been selected yet, so there is no corpus to "
    f"load. Selection criterion (pre-registered): {KAGGLE_SELECTION_CRITERION} "
    "The corpus is locked before the noise floor and unchangeable after, so this "
    "adapter stays unimplemented until all five are chosen at once."
)


@dataclass(frozen=True, slots=True)
class KaggleAdapter:
    """Not implemented. See the module docstring for why, and for the criterion.

    Satisfies :class:`~turing.research.problems.adapter.ProblemAdapter`
    structurally so the registry and the round loop can be written against the
    finished shape today. Both methods raise.
    """

    data_root: Path

    @property
    def problem_type(self) -> ProblemType:
        return ProblemType.KAGGLE

    def load(self) -> tuple[Problem, ...]:
        """Always raises: the five competitions are not selected."""
        raise NotImplementedError(_NOT_SELECTED)

    async def materialise_workspace(self, problem: Problem, destination: Path) -> Path:
        """Always raises: there is no Kaggle problem to materialise."""
        raise NotImplementedError(_NOT_SELECTED)
