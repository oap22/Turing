"""Solver settings: the workspace root and the cap, from the environment."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from turing.research.solver import SolverSettings

if TYPE_CHECKING:
    import pytest


def _settings(**overrides: object) -> SolverSettings:
    return SolverSettings(_env_file=None, **overrides)  # type: ignore[arg-type]


class TestDefaults:
    def test_the_workspace_root_defaults_to_the_documented_location(self) -> None:
        assert _settings().research_workspace_root == Path.home() / "turing-workspace"

    def test_a_tilde_is_expanded_so_the_path_is_usable(self) -> None:
        settings = _settings(research_checkpoint_db=Path("~/somewhere/checkpoints.db"))
        assert settings.research_checkpoint_db.is_absolute()
        assert "~" not in str(settings.research_checkpoint_db)

    def test_the_default_cap_is_positive_on_every_dimension(self) -> None:
        cap = _settings().default_cap()
        assert cap.max_steps > 0
        assert cap.max_tokens > 0
        assert cap.max_wall_clock_seconds > 0
        assert cap.extension_count == 0

    def test_the_default_policy_has_no_target_score(self) -> None:
        """A bar belongs to a problem, not to a global setting."""
        assert _settings().default_policy().target_score is None
        assert _settings().default_policy(target_score=2.0).target_score == 2.0


class TestEnvironment:
    def test_settings_come_from_turing_prefixed_variables(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("TURING_RESEARCH_WORKSPACE_ROOT", str(tmp_path / "ws"))
        monkeypatch.setenv("TURING_RESEARCH_CAP_MAX_STEPS", "7")
        monkeypatch.setenv("TURING_RESEARCH_CAP_MAX_TOKENS", "1234")
        monkeypatch.setenv("TURING_RESEARCH_CAP_MAX_WALL_CLOCK_SECONDS", "90.5")
        monkeypatch.setenv("TURING_RESEARCH_ESCALATE_ON_CAP_EXHAUSTED", "false")
        monkeypatch.setenv("TURING_RESEARCH_PATIENCE", "3")

        settings = _settings()

        assert settings.research_workspace_root == tmp_path / "ws"
        assert settings.default_cap().max_steps == 7
        assert settings.default_cap().max_tokens == 1234
        assert settings.default_cap().max_wall_clock_seconds == 90.5
        policy = settings.default_policy()
        assert policy.escalate_on_cap_exhausted is False
        assert policy.patience == 3

    def test_unrelated_turing_variables_are_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TURING_NODE_NAME", "pi-alpha")
        assert _settings().research_cap_max_steps > 0
