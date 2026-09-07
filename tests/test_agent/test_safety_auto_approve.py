"""safety_auto_approve_high_risk: turns NEEDS_CONFIRMATION into APPROVED, nothing more."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from turing.agent.safety import SafetyDecision, SafetyGate


def _gate(
    auto: bool,
    admins: list[str] | None = None,
    single_operator: str | None = None,
) -> SafetyGate:
    return SafetyGate(
        SimpleNamespace(
            admin_user_ids=admins or [],
            safety_auto_approve_high_risk=auto,
            safety_single_operator_user_id=single_operator,
        )
    )


class TestAutoApproveHighRisk:
    @pytest.mark.asyncio
    async def test_default_off_still_needs_confirmation(self) -> None:
        gate = _gate(auto=False)
        result = await gate.check("shell", {"command": "systemctl restart turing"}, "u1")
        assert result.decision == SafetyDecision.NEEDS_CONFIRMATION

    @pytest.mark.asyncio
    async def test_flag_without_operator_authority_still_needs_confirmation(self) -> None:
        gate = _gate(auto=True)
        result = await gate.check("shell", {"command": "systemctl restart turing"}, "u1")
        assert result.decision == SafetyDecision.NEEDS_CONFIRMATION

    @pytest.mark.asyncio
    async def test_flag_approves_high_risk_shell_for_single_operator(self) -> None:
        gate = _gate(auto=True, single_operator="u1")
        result = await gate.check("shell", {"command": "systemctl restart turing"}, "u1")
        assert result.decision == SafetyDecision.APPROVED
        assert result.risk_level == "high"
        assert "safety_auto_approve_high_risk" in result.reason

    @pytest.mark.asyncio
    async def test_flag_approves_process_kill_for_non_admin(self) -> None:
        # The process tool has its own admin short-circuit ahead of the
        # risk-based step; the flag must cover that path too or it is a
        # half-switch.
        gate = _gate(auto=True, single_operator="u1")
        result = await gate.check("process", {"action": "kill_process", "pid": 1234}, "u1")
        assert result.decision == SafetyDecision.APPROVED

    @pytest.mark.asyncio
    async def test_flag_never_overrides_the_deny_list(self) -> None:
        gate = _gate(auto=True)
        result = await gate.check("shell", {"command": "rm -rf /"}, "u1")
        assert result.decision == SafetyDecision.DENIED

    @pytest.mark.asyncio
    async def test_admin_reason_wins_when_both_apply(self) -> None:
        gate = _gate(auto=True, admins=["owner"])
        result = await gate.check("shell", {"command": "systemctl restart turing"}, "owner")
        assert result.decision == SafetyDecision.APPROVED
        assert result.reason == "Approved: user is admin"

    @pytest.mark.asyncio
    async def test_config_without_the_field_behaves_as_off(self) -> None:
        gate = SafetyGate(SimpleNamespace(admin_user_ids=[]))
        result = await gate.check("shell", {"command": "systemctl restart turing"}, "u1")
        assert result.decision == SafetyDecision.NEEDS_CONFIRMATION
