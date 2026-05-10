"""Capability gate end-to-end tests (issue #97 / ADR 0003).

Covers the 8-step gate, audit log, replay handling, fullmatch semantics,
deny-by-default, and the worker ToolRequestClient round-trip.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from turing.coordinator.capability_token import (
    DENY_ALL_REGEX,
    CapabilityScope,
    CapabilityTokenIssuer,
    CapabilityVerifier,
    TokenIssuer,
)
from turing.coordinator.capability_token.audit import GateAuditLog
from turing.coordinator.capability_token.gate import Gate
from turing.coordinator.capability_token.transport import (
    GateService,
    ToolRequest,
    request_subject,
)
from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.transport.bus import InMemoryBus
from turing.transport.envelope import MeshMessage
from turing.transport.signed_transport import SignedTransport
from turing.transport.signer import MessageSigner
from turing.worker.tool_request_client import ToolRequestClient

if TYPE_CHECKING:
    from pathlib import Path

    from turing.coordinator.capability_token.token import CapabilityToken

# ── helpers ─────────────────────────────────────────────────────────────


class _FakeLifecycle:
    def __init__(self, state: SubtaskState | None = SubtaskState.RUNNING) -> None:
        self._state = state

    def state_of(self, subtask_id: str) -> SubtaskState:
        if self._state is None:
            raise KeyError(subtask_id)
        return self._state


def _scope(
    *,
    subtask_id="st_a",
    task_id="tsk_x",
    regex=r"echo .*",
    expires_at_ms=10_000_000,
    timeout_s=30,
    issued_at_ms=1_000,
) -> CapabilityScope:
    return CapabilityScope(
        subtask_id=subtask_id,
        task_id=task_id,
        allowed_commands_regex=regex,
        timeout_s=timeout_s,
        expires_at_ms=expires_at_ms,
        issued_at_ms=issued_at_ms,
    )


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    return tmp_path / "workspaces"


@pytest.fixture
def coord_signer() -> MessageSigner:
    return MessageSigner.generate()


@pytest.fixture
def gate_setup(workspace_root: Path, coord_signer: MessageSigner):
    audit = GateAuditLog()
    verifier = CapabilityVerifier(trusted_issuers=[coord_signer.public_key])
    lifecycle = _FakeLifecycle()
    gate = Gate(
        verifier=verifier,
        lifecycle=lifecycle,
        workspace_root=workspace_root,
        audit_log=audit,
        now_ms=lambda: 1_000_000,
    )
    return gate, audit, lifecycle


def _token(coord_signer: MessageSigner, **scope_kwargs) -> CapabilityToken:
    return CapabilityTokenIssuer(signer=coord_signer).issue(_scope(**scope_kwargs))


def _request(
    token: CapabilityToken | None,
    *,
    command: str = "echo hi",
    request_id="r1",
    subtask_id="st_a",
    tool="shell",
) -> ToolRequest:
    return ToolRequest(
        request_id=request_id,
        subtask_id=subtask_id,
        tool=tool,
        args={"command": command},
        capability_token=token.to_dict() if token is not None else None,
    )


# ── Gate (steps 3-8) ────────────────────────────────────────────────────


class TestGate:
    @pytest.mark.asyncio
    async def test_allowed_command_runs_in_workspace(
        self, gate_setup, coord_signer, workspace_root
    ):
        gate, audit, _ = gate_setup
        token = _token(coord_signer, regex=r"echo .*")
        decision = await gate.evaluate(
            request=_request(token, command="echo hello-from-gate"),
            worker_id="w1",
            token=token,
        )
        assert decision.outcome == "ALLOWED"
        assert decision.exit_code == 0
        assert "hello-from-gate" in decision.stdout
        # workspace dir was created under task/subtask
        assert (workspace_root / "tsk_x" / "st_a").is_dir()
        # audit row written with token fingerprint
        assert len(audit) == 1
        row = audit.rows()[0]
        assert row.outcome == "ALLOWED"
        assert row.token_fingerprint == token.fingerprint()

    @pytest.mark.asyncio
    async def test_unsupported_tool_rejected(self, gate_setup, coord_signer):
        gate, _, _ = gate_setup
        token = _token(coord_signer, regex=r".*")
        decision = await gate.evaluate(
            request=_request(token, tool="vault_query"),
            worker_id="w1",
            token=token,
        )
        assert decision.outcome == "DENIED"
        assert decision.reason == "unsupported-tool"

    @pytest.mark.asyncio
    async def test_token_signature_invalid(self, gate_setup, coord_signer):
        gate, _, _ = gate_setup
        # Sign with a rogue key the gate doesn't trust.
        rogue = MessageSigner.generate()
        token = CapabilityTokenIssuer(signer=rogue).issue(_scope())
        decision = await gate.evaluate(request=_request(token), worker_id="w1", token=token)
        assert decision.outcome == "DENIED"
        assert decision.reason == "token-signature"

    @pytest.mark.asyncio
    async def test_subtask_mismatch(self, gate_setup, coord_signer):
        gate, _, _ = gate_setup
        token = _token(coord_signer, subtask_id="st_other")
        decision = await gate.evaluate(
            request=_request(token, subtask_id="st_a"),
            worker_id="w1",
            token=token,
        )
        assert decision.outcome == "DENIED"
        assert decision.reason == "subtask-mismatch"

    @pytest.mark.asyncio
    async def test_subtask_state_failed_blocks_even_if_token_valid(
        self, workspace_root, coord_signer
    ):
        # Token still within expires_at, but subtask = FAILED → DENIED.
        audit = GateAuditLog()
        verifier = CapabilityVerifier(trusted_issuers=[coord_signer.public_key])
        gate = Gate(
            verifier=verifier,
            lifecycle=_FakeLifecycle(SubtaskState.FAILED),
            workspace_root=workspace_root,
            audit_log=audit,
            now_ms=lambda: 1_000_000,  # well before any expires_at
        )
        token = _token(coord_signer, regex=r".*", expires_at_ms=999_999_999_999)
        decision = await gate.evaluate(request=_request(token), worker_id="w1", token=token)
        assert decision.outcome == "DENIED"
        assert decision.reason == "subtask-not-running"

    @pytest.mark.asyncio
    async def test_expired_token_denied(self, workspace_root, coord_signer):
        audit = GateAuditLog()
        verifier = CapabilityVerifier(trusted_issuers=[coord_signer.public_key])
        gate = Gate(
            verifier=verifier,
            lifecycle=_FakeLifecycle(),
            workspace_root=workspace_root,
            audit_log=audit,
            now_ms=lambda: 999_999_999,
        )
        token = _token(coord_signer, regex=r".*", expires_at_ms=1)
        decision = await gate.evaluate(request=_request(token), worker_id="w1", token=token)
        assert decision.outcome == "DENIED"
        assert decision.reason == "expired"

    @pytest.mark.asyncio
    async def test_command_not_allowed_uses_fullmatch(self, gate_setup, coord_signer):
        gate, _, _ = gate_setup
        # `cat .*` must NOT permit `cat foo\n; rm -rf /` (would pass with re.match).
        token = _token(coord_signer, regex=r"cat .*")
        decision = await gate.evaluate(
            request=_request(token, command="cat foo\n; rm -rf /"),
            worker_id="w1",
            token=token,
        )
        assert decision.outcome == "DENIED"
        assert decision.reason == "command-not-allowed"

    @pytest.mark.asyncio
    async def test_v1_deny_by_default_blocks_everything(self, gate_setup, coord_signer):
        gate, _, _ = gate_setup
        token = _token(coord_signer, regex=DENY_ALL_REGEX)
        for cmd in ["echo hi", "ls", "true", "anything", ""]:
            decision = await gate.evaluate(
                request=_request(token, command=cmd, request_id=f"r-{cmd}"),
                worker_id="w1",
                token=token,
            )
            assert decision.outcome == "DENIED"
            assert decision.reason == "command-not-allowed"

    @pytest.mark.asyncio
    async def test_token_missing(self, gate_setup):
        gate, _, _ = gate_setup
        decision = await gate.evaluate(request=_request(None), worker_id="w1", token=None)
        assert decision.outcome == "DENIED"
        assert decision.reason == "token-missing"


# ── Replay (step 2) ─────────────────────────────────────────────────────


@pytest.fixture
def signed_transports():
    bus = InMemoryBus()
    coord_sig = MessageSigner.generate()
    worker_sig = MessageSigner.generate()
    coord = SignedTransport(
        bus=bus,
        signer=coord_sig,
        trusted_keys=[worker_sig.public_key],
        now_ms=lambda: 1_700_000_000_000,
    )
    worker = SignedTransport(
        bus=bus,
        signer=worker_sig,
        trusted_keys=[coord_sig.public_key],
        now_ms=lambda: 1_700_000_000_000,
    )
    return bus, coord, worker, coord_sig, worker_sig


class TestReplay:
    @pytest.mark.asyncio
    async def test_replay_dropped_no_second_audit_row(self, signed_transports, workspace_root):
        _, coord, worker, coord_sig, _ = signed_transports
        audit = GateAuditLog()
        verifier = CapabilityVerifier(trusted_issuers=[coord_sig.public_key])
        gate = Gate(
            verifier=verifier,
            lifecycle=_FakeLifecycle(),
            workspace_root=workspace_root,
            audit_log=audit,
            now_ms=lambda: 1_700_000_000_000,
        )
        service = GateService(
            transport=coord,
            gate=gate,
            sender_id="coordinator",
            now_ms=lambda: 1_700_000_000_000,
        )
        await service.subscribe("st_a")

        token = CapabilityTokenIssuer(signer=coord_sig).issue(
            _scope(regex=r"echo .*", expires_at_ms=1_800_000_000_000)
        )
        envelope = ToolRequest(
            request_id="dup-1",
            subtask_id="st_a",
            tool="shell",
            args={"command": "echo hi"},
            capability_token=token.to_dict(),
        )
        msg = MeshMessage(
            request_id="dup-1",
            sender_id="worker-1",
            subject=request_subject("st_a"),
            payload=json.dumps(envelope.to_dict()).encode("utf-8"),
            timestamp_ms=1_700_000_000_000,
        )
        await worker.publish(msg)
        # Republish the same frame (same request_id)
        await worker.publish(msg)

        # AC: "second copy DROPPED, no second audit row." SignedTransport's
        # replay window rejects the duplicate frame at the wire layer (one
        # gate, one audit log — replay never reaches the gate path).
        rows = audit.rows()
        assert len(rows) == 1
        assert rows[0].outcome == "ALLOWED"
        assert rows[0].request_id == "dup-1"


# ── Worker ToolRequestClient round-trip ────────────────────────────────


class TestToolRequestClient:
    @pytest.mark.asyncio
    async def test_round_trip_allowed(self, signed_transports, workspace_root):
        _, coord, worker, coord_sig, _ = signed_transports
        audit = GateAuditLog()
        verifier = CapabilityVerifier(trusted_issuers=[coord_sig.public_key])
        gate = Gate(
            verifier=verifier,
            lifecycle=_FakeLifecycle(),
            workspace_root=workspace_root,
            audit_log=audit,
            now_ms=lambda: 1_700_000_000_000,
        )
        service = GateService(
            transport=coord, gate=gate, sender_id="coordinator", now_ms=lambda: 1_700_000_000_000
        )
        await service.subscribe("st_a")

        token = CapabilityTokenIssuer(signer=coord_sig).issue(
            _scope(regex=r"echo .*", expires_at_ms=1_800_000_000_000)
        )
        client = ToolRequestClient(
            transport=worker, sender_id="worker-1", now_ms=lambda: 1_700_000_000_000
        )
        result = await asyncio.wait_for(
            client.request(
                subtask_id="st_a",
                tool="shell",
                args={"command": "echo from-worker"},
                token=token,
                timeout_s=5.0,
            ),
            timeout=5,
        )
        assert result.status == "ALLOWED"
        assert "from-worker" in result.output

    @pytest.mark.asyncio
    async def test_round_trip_unsupported_tool(self, signed_transports, workspace_root):
        _, coord, worker, coord_sig, _ = signed_transports
        audit = GateAuditLog()
        verifier = CapabilityVerifier(trusted_issuers=[coord_sig.public_key])
        gate = Gate(
            verifier=verifier,
            lifecycle=_FakeLifecycle(),
            workspace_root=workspace_root,
            audit_log=audit,
            now_ms=lambda: 1_700_000_000_000,
        )
        service = GateService(
            transport=coord, gate=gate, sender_id="coordinator", now_ms=lambda: 1_700_000_000_000
        )
        await service.subscribe("st_a")

        token = CapabilityTokenIssuer(signer=coord_sig).issue(
            _scope(regex=r".*", expires_at_ms=1_800_000_000_000)
        )
        client = ToolRequestClient(
            transport=worker, sender_id="worker-1", now_ms=lambda: 1_700_000_000_000
        )
        result = await asyncio.wait_for(
            client.request(
                subtask_id="st_a",
                tool="vault_query",
                args={"query": "x"},
                token=token,
                timeout_s=5.0,
            ),
            timeout=5,
        )
        assert result.status == "DENIED"
        assert result.error == "unsupported-tool"


# ── TokenIssuer ↔ Orchestrator wiring ─────────────────────────────────


class TestTokenIssuerWithOrchestratorContract:
    def test_issuer_uses_v1_deny_by_default_and_proper_lifetime(self, coord_signer):
        from turing.coordinator.dispatch.envelopes import SubtaskDispatch
        from turing.coordinator.planner.schema import DAG

        dag = DAG.model_validate(
            {
                "task_id": "tsk_y",
                "version": 1,
                "subtasks": [
                    {
                        "id": "st_y",
                        "specialty_required": "research-summarize",
                        "prompt": "p",
                        "depends_on": [],
                        "required_tools": [],
                        "inputs": {},
                        "output_key": "workspace://tsk_y/st_y/result",
                        "max_retries": 1,
                        "timeout_s": 60,
                    }
                ],
            }
        )
        dispatch = SubtaskDispatch(
            subtask_id="st_y",
            task_id="tsk_y",
            specialty="research-summarize",
            prompt="p",
            source_inputs=[],
            deadline_ms=2_000_000,
        )
        issuer = TokenIssuer(signer=coord_signer, now_ms=lambda: 100_000)
        token = issuer.issue_for(subtask=dag.subtasks[0], dispatch=dispatch)
        assert token.scope.allowed_commands_regex == DENY_ALL_REGEX
        assert token.scope.expires_at_ms == dispatch.deadline_ms + 30_000
        assert token.scope.timeout_s == 30
        assert token.scope.issued_at_ms == 100_000
        # Orchestrator-issued token rejects every command (deny-by-default AC).
        verifier = CapabilityVerifier(trusted_issuers=[coord_signer.public_key])
        from turing.coordinator.capability_token.token import ScopeViolationError

        for cmd in ["echo hi", "ls", "true"]:
            with pytest.raises(ScopeViolationError):
                verifier.authorize(token, command=cmd, now_ms=200_000)
