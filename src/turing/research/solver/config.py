"""Solver settings, via ``TURING_``-prefixed environment variables.

A separate :class:`pydantic_settings.BaseSettings` rather than fields on
:class:`turing.config.TuringConfig`: the research agent is a distinct program
that happens to live in this repo, and the coordinator/worker/mesh config it
would otherwise sit beside is dormant under the new framing. Same prefix, same
``.env`` file, so operating it feels identical.

The cap defaults are the runaway-loop brake that replaced the retired dollar
``BudgetGate``. They are deliberately conservative — an unattended overnight
loop that misjudges its budget wastes a subscription window, and the cost of a
cap that is too tight is one ``extend_cap`` decision.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from turing.research.contracts import Cap
from turing.research.solver.models import SolverPolicy

__all__ = ["SolverSettings"]


class SolverSettings(BaseSettings):
    """Configuration for the within-project solver."""

    model_config = SettingsConfigDict(
        env_prefix="TURING_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Workspace isolation ──────────────────────────────────────────────
    research_workspace_root: Path = Field(
        default=Path("~/turing-workspace"),
        description=(
            "Root of every attempt workspace. Owned by the separate 'turing' "
            "macOS user, which is the OS-enforced half of workspace isolation; "
            "this setting only says where that directory is."
        ),
    )
    research_checkpoint_db: Path = Field(
        default=Path("./data/research-attempts.db"),
        description="SQLite file holding attempt checkpoints and iteration journals",
    )

    # ── Per-project cap (steps / tokens / wall clock) ────────────────────
    research_cap_max_steps: int = Field(
        default=40,
        description="Refinement iterations allowed per attempt before the cap trips",
    )
    research_cap_max_tokens: int = Field(
        default=2_000_000,
        description="Model tokens allowed per attempt before the cap trips",
    )
    research_cap_max_wall_clock_seconds: float = Field(
        default=4 * 60 * 60,
        description="Wall-clock seconds allowed per attempt before the cap trips",
    )

    # ── Loop policy ──────────────────────────────────────────────────────
    research_escalate_on_cap_exhausted: bool = Field(
        default=True,
        description=(
            "Ask the operator for extend_cap when the cap trips. Set false to "
            "land in FAILED_WITHIN_CAP directly, which keeps escalations-per-round "
            "a measure of autonomy rather than of cap sizing."
        ),
    )
    research_patience: int | None = Field(
        default=None,
        description=(
            "Escalate NO_VIABLE_APPROACH after this many non-improving iterations. "
            "None refines until the cap. Never terminates an attempt."
        ),
    )
    research_regression_patience: int | None = Field(
        default=None,
        description=(
            "Escalate REPEATED_REGRESSION after this many consecutively worse "
            "iterations. None disables the check."
        ),
    )

    @field_validator("research_workspace_root", "research_checkpoint_db")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        return value.expanduser()

    def default_cap(self) -> Cap:
        """The cap an attempt starts with when its problem declares none."""
        return Cap(
            max_steps=self.research_cap_max_steps,
            max_tokens=self.research_cap_max_tokens,
            max_wall_clock_seconds=self.research_cap_max_wall_clock_seconds,
        )

    def default_policy(self, *, target_score: float | None = None) -> SolverPolicy:
        """The loop policy these settings describe.

        ``target_score`` is not a setting: it is the frozen bar for one
        problem, on that problem's own scale, and lives with the problem. A
        global default would be a bar the corpus did not agree to.
        """
        return SolverPolicy(
            target_score=target_score,
            patience=self.research_patience,
            regression_patience=self.research_regression_patience,
            escalate_on_cap_exhausted=self.research_escalate_on_cap_exhausted,
        )
