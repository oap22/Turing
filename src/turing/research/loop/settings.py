"""Env-driven settings for the research loop, on the repo's ``TURING_`` prefix.

Separate from :class:`turing.config.TuringConfig` because the research loop is
a distinct program with its own lifetime, but sharing the prefix and the two
ntfy field names so one ``.env`` configures both: escalation delivery reuses
the coordinator's ntfy client and therefore its topic.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["ResearchLoopSettings"]


class ResearchLoopSettings(BaseSettings):
    """Configuration for an unattended research-loop run.

    Every field is settable as ``TURING_<FIELD>``. Tests construct this with
    ``_env_file=None`` so a developer's real ``.env`` never leaks in.
    """

    model_config = SettingsConfigDict(
        env_prefix="TURING_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Artifacts ────────────────────────────────────────────────────────
    research_results_root: Path = Field(
        default=Path.home() / "research-results",
        description="Directory holding loop-<slug>/ trajectories. Home-anchored "
        "rather than repo-relative so the loop can run from any project and "
        "still land where the desktop app watches for metrics and plots",
    )
    research_workspace_root: Path = Field(
        default=Path.home() / "turing-workspace",
        description="Parent of the per-attempt working directories. The brief "
        "puts this under a separate `turing` macOS user: a directory is a "
        "convention, a user account is a boundary the OS enforces",
    )

    # ── Escalation delivery (reuses the coordinator's ntfy fallback) ─────
    operator_ntfy_topic: str | None = Field(
        default=None,
        description="ntfy topic that receives escalations; unset disables the push "
        "(the loop still suspends and still waits for a decision file)",
    )
    coordinator_ntfy_base_url: str | None = Field(
        default=None,
        description="ntfy server base URL, e.g. http://surface.<tailnet>.ts.net:8090",
    )
    research_escalation_poll_seconds: float = Field(
        default=5.0,
        gt=0,
        description="How often the decision inbox is checked for a reply",
    )
    research_escalation_repush_seconds: float | None = Field(
        default=1800.0,
        description="How often the operator is reminded that the loop is suspended. "
        "None disables reminders; the loop still never times out into a default "
        "verdict, because that would erase a human-gate-load event",
    )
