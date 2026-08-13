"""ProposalAdapter: ModelBackend → ProposalBackend, the missing seam."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from turing.research.backends import (
    FakeBackend,
    ProposalAdapter,
    Usage,
    encode_proposal,
)
from turing.research.backends.errors import BackendError, BackendProtocolError
from turing.research.backends.fake import ScriptedTurn
from turing.research.contracts import CapConsumption, ProblemType
from turing.research.solver.models import ProposalContext
from turing.research.solver.protocols import ProposalBackend, ResumableBackend

if TYPE_CHECKING:
    from pathlib import Path


def _context(workspace: Path, *, iteration: int = 0) -> ProposalContext:
    return ProposalContext(
        problem_id="synth",
        problem_type=ProblemType.SPEEDUP,
        goal="raise the number",
        score_scale="speedup_ratio",
        workspace=workspace,
        iteration_index=iteration,
        seed=7,
        remaining=CapConsumption(steps=3, tokens=1000, wall_clock_seconds=60.0),
    )


class TestSeam:
    def test_satisfies_both_solver_protocols(self) -> None:
        adapter = ProposalAdapter(FakeBackend.replying("{}"))
        assert isinstance(adapter, ProposalBackend)
        assert isinstance(adapter, ResumableBackend)

    async def test_tokens_come_from_the_ledger_not_the_json(self, tmp_path: Path) -> None:
        """A model-authored tokens field must not be what the cap reads."""
        fake = FakeBackend(
            [
                ScriptedTurn(
                    text=encode_proposal(
                        rationale="write it",
                        edits=(("solution.txt", "2.0"),),
                    ),
                    usage=Usage(input_tokens=10, output_tokens=5),
                )
            ]
        )
        proposal = await ProposalAdapter(fake).propose(_context(tmp_path))
        assert proposal.tokens == 15
        assert '"tokens"' not in fake.calls[0].messages[0].text

    async def test_the_prompt_never_names_a_verifier(self, tmp_path: Path) -> None:
        fake = FakeBackend.replying(encode_proposal(edits=(("a.txt", "x"),)))
        await ProposalAdapter(fake).propose(_context(tmp_path))
        request = fake.calls[0]
        blob = request.system + request.messages[0].text
        assert "Verifier" not in blob
        assert "Problem(" not in blob
        assert "synth" in blob
        assert "raise the number" in blob

    async def test_parses_edits_and_forwards_resume(self, tmp_path: Path) -> None:
        fake = FakeBackend.replying(
            encode_proposal(rationale="try 2", edits=(("solution.txt", "2.0"),))
        )
        adapter = ProposalAdapter(fake)
        proposal = await adapter.propose(_context(tmp_path))
        assert proposal.rationale == "try 2"
        assert proposal.edits[0].relative_path == "solution.txt"
        assert proposal.edits[0].content == "2.0"
        assert proposal.no_viable_approach is False
        token = adapter.checkpoint()
        other = ProposalAdapter(FakeBackend.replying(encode_proposal()))
        other.restore(token)
        assert other.backend.ledger.consumption.tokens == fake.ledger.consumption.tokens

    async def test_no_viable_approach_is_advisory(self, tmp_path: Path) -> None:
        fake = FakeBackend.replying(encode_proposal(rationale="stuck", no_viable_approach=True))
        proposal = await ProposalAdapter(fake).propose(_context(tmp_path))
        assert proposal.no_viable_approach is True
        assert proposal.edits == ()

    async def test_malformed_json_is_a_protocol_error(self, tmp_path: Path) -> None:
        fake = FakeBackend.replying("not a proposal")
        with pytest.raises(BackendProtocolError, match="JSON"):
            await ProposalAdapter(fake).propose(_context(tmp_path))

    async def test_malformed_json_carries_accounted_usage(self, tmp_path: Path) -> None:
        """generate() spent; the error must carry that, not leave it on the ledger."""
        fake = FakeBackend(
            [
                ScriptedTurn(
                    text="not a proposal",
                    usage=Usage(input_tokens=10, output_tokens=5),
                )
            ]
        )
        with pytest.raises(BackendProtocolError, match="JSON") as caught:
            await ProposalAdapter(fake).propose(_context(tmp_path))
        assert caught.value.usage is not None
        assert caught.value.usage.total_tokens == 15
        assert fake.ledger.tokens == 15

    async def test_generate_failure_carries_estimated_usage(self, tmp_path: Path) -> None:
        """generate() spent an estimate; the error must carry that, not leave it on the ledger."""
        fake = FakeBackend([ScriptedTurn(error=BackendError("provider down"))])
        with pytest.raises(BackendError, match="provider down") as caught:
            await ProposalAdapter(fake).propose(_context(tmp_path))
        assert caught.value.usage is not None
        assert caught.value.usage.estimated is True
        assert caught.value.usage.total_tokens == fake.ledger.tokens
        assert fake.ledger.tokens > 0
        assert fake.ledger.steps == 1

    async def test_explicit_zero_usage_falls_back_to_an_estimate(self, tmp_path: Path) -> None:
        """BackendError(usage=Usage()) is unknown spend, not a free call."""
        fake = FakeBackend([ScriptedTurn(error=BackendError("provider down", usage=Usage()))])
        with pytest.raises(BackendError, match="provider down") as caught:
            await ProposalAdapter(fake).propose(_context(tmp_path))
        assert caught.value.usage is not None
        assert caught.value.usage.estimated is True
        assert caught.value.usage.total_tokens == fake.ledger.tokens
        assert fake.ledger.tokens > 0
        assert fake.ledger.steps == 1

    async def test_foreign_spend_on_a_non_backend_error_is_replaced(self, tmp_path: Path) -> None:
        """A crash whose object already has usage/tokens must still carry the ledger."""

        class SdkError(RuntimeError):
            def __init__(self) -> None:
                super().__init__("sdk blew up")
                self.usage = Usage(input_tokens=1)
                self.tokens = 1

        fake = FakeBackend([ScriptedTurn(error=SdkError())])
        with pytest.raises(SdkError) as caught:
            await ProposalAdapter(fake).propose(_context(tmp_path))
        charged = fake.ledger.tokens
        assert charged > 1
        assert caught.value.usage.total_tokens == charged
        assert caught.value.tokens == charged
        assert fake.ledger.steps == 1

    async def test_read_only_usage_on_a_non_backend_error_is_enveloped(
        self, tmp_path: Path
    ) -> None:
        """propose() must surface the ledger charge, not a frozen usage of 1."""

        class SdkError(RuntimeError):
            @property
            def usage(self) -> Usage:
                return Usage(input_tokens=0, output_tokens=1)

        fake = FakeBackend([ScriptedTurn(error=SdkError("sdk blew up"))])
        with pytest.raises(BackendError) as caught:
            await ProposalAdapter(fake).propose(_context(tmp_path))
        charged = fake.ledger.tokens
        assert charged > 1
        assert caught.value.usage is not None
        assert caught.value.usage.total_tokens == charged
        assert isinstance(caught.value.__cause__, SdkError)
        assert fake.ledger.steps == 1

    async def test_empty_reply_is_a_protocol_error(self, tmp_path: Path) -> None:
        fake = FakeBackend.replying("   ")
        with pytest.raises(BackendProtocolError, match="empty"):
            await ProposalAdapter(fake).propose(_context(tmp_path))

    async def test_fenced_json_is_accepted(self, tmp_path: Path) -> None:
        body = encode_proposal(edits=(("x.txt", "ok"),))
        fake = FakeBackend.replying(f"```json\n{body}\n```")
        proposal = await ProposalAdapter(fake).propose(_context(tmp_path))
        assert proposal.edits[0].content == "ok"
