"""Escalation delivery: the push out, the file drop in, and the vocabulary.

The load-bearing test in this file is
``test_advice_bearing_reply_is_rejected``: the operator's channel back into the
run is three values plus numbers, and a reply carrying guidance must be refused
rather than partially honoured. Free-form advice would make the operator the
improvement mechanism and confound every later delta.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from turing.research.contracts import (
    Cap,
    CapConsumption,
    EscalationDecision,
    EscalationProtocolError,
    EscalationReason,
    EscalationRequest,
    EscalationVerdict,
    VerificationResult,
)
from turing.research.loop.cli import main as cli_main
from turing.research.loop.escalation import (
    FileDropDecisionInbox,
    NtfyEscalationNotifier,
    OperatorEscalationChannel,
    decode_decision,
    encode_decision,
    encode_request,
)

from .conftest import FakeClock, RecordingNtfyClient

if TYPE_CHECKING:
    from pathlib import Path

CAP = Cap(max_steps=10, max_tokens=1000, max_wall_clock_seconds=60.0)


def make_request(request_id: str = "esc-1") -> EscalationRequest:
    return EscalationRequest(
        request_id=request_id,
        problem_id="s1",
        attempt_id="a1",
        round_id="r00",
        reason=EscalationReason.NO_VIABLE_APPROACH,
        summary="tried three approaches, all regressed",
        cap=CAP,
        consumed=CapConsumption(steps=10, tokens=900, wall_clock_seconds=30.0),
        created_at_ms=1,
        best_result=VerificationResult(
            problem_id="s1",
            verifier_id="v-s1",
            score=1.2,
            passed_correctness=True,
            score_scale="speedup_ratio",
        ),
    )


class TestDecodeDecision:
    @pytest.mark.parametrize("verdict", ["continue", "abandon"])
    def test_the_two_bare_verdicts_round_trip(self, verdict: str) -> None:
        decision = decode_decision({"request_id": "esc-1", "verdict": verdict, "decided_at_ms": 5})
        assert decision.verdict.value == verdict
        assert decision.cap_extension is None

    def test_extend_cap_carries_numbers_only(self) -> None:
        decision = decode_decision(
            {
                "request_id": "esc-1",
                "verdict": "extend_cap",
                "decided_at_ms": 5,
                "cap_extension": {"extra_steps": 10, "extra_tokens": 100},
            }
        )
        assert decision.cap_extension is not None
        assert decision.cap_extension.extra_steps == 10
        assert decision.apply_to(CAP).max_steps == 20

    def test_advice_bearing_reply_is_rejected(self) -> None:
        """The whole point: guidance is not part of the protocol."""
        with pytest.raises(EscalationProtocolError, match="unsupported field"):
            decode_decision(
                {
                    "request_id": "esc-1",
                    "verdict": "continue",
                    "advice": "try gradient boosting on that one",
                }
            )

    def test_advice_smuggled_into_the_cap_extension_is_rejected(self) -> None:
        with pytest.raises(EscalationProtocolError, match="unsupported field"):
            decode_decision(
                {
                    "request_id": "esc-1",
                    "verdict": "extend_cap",
                    "cap_extension": {"extra_steps": 5, "hint": "use xgboost"},
                }
            )

    def test_an_unknown_verdict_is_rejected(self) -> None:
        with pytest.raises(EscalationProtocolError, match="not an operator verdict"):
            decode_decision({"request_id": "esc-1", "verdict": "try_harder"})

    def test_a_decision_for_another_request_is_rejected(self) -> None:
        with pytest.raises(EscalationProtocolError, match="not the open request"):
            decode_decision(
                {"request_id": "esc-2", "verdict": "continue"}, expected_request_id="esc-1"
            )

    def test_extend_cap_without_numbers_is_rejected(self) -> None:
        with pytest.raises(EscalationProtocolError, match="EXTEND_CAP requires"):
            decode_decision({"request_id": "esc-1", "verdict": "extend_cap"})

    def test_continue_carrying_a_budget_is_rejected(self) -> None:
        with pytest.raises(EscalationProtocolError, match="must not carry"):
            decode_decision(
                {
                    "request_id": "esc-1",
                    "verdict": "continue",
                    "cap_extension": {"extra_steps": 5},
                }
            )

    def test_a_non_object_reply_is_rejected(self) -> None:
        with pytest.raises(EscalationProtocolError, match="must be a JSON object"):
            decode_decision("continue")

    def test_encode_decode_round_trip(self) -> None:
        original = EscalationDecision(
            request_id="esc-1", verdict=EscalationVerdict.ABANDON, decided_at_ms=99
        )
        assert decode_decision(encode_decision(original)) == original

    def test_the_encoded_decision_has_no_free_text_field(self) -> None:
        payload = encode_decision(
            EscalationDecision(
                request_id="esc-1", verdict=EscalationVerdict.CONTINUE, decided_at_ms=1
            )
        )
        assert set(payload) == {"request_id", "verdict", "decided_at_ms"}


class TestRequestEncoding:
    def test_the_request_carries_rich_context_outward(self) -> None:
        body = encode_request(make_request())
        assert body["summary"].startswith("tried three approaches")
        assert body["best_result"]["score"] == pytest.approx(1.2)
        assert body["reply_with"] == ["abandon", "continue", "extend_cap"]


class TestFileDropInbox:
    async def test_publish_writes_the_request(self, tmp_path: Path) -> None:
        inbox = FileDropDecisionInbox(tmp_path, clock=FakeClock())
        path = await inbox.publish(make_request())
        assert path.name == "esc-1.request.json"
        assert json.loads(path.read_text())["problem_id"] == "s1"

    async def test_poll_returns_none_until_a_reply_lands(self, tmp_path: Path) -> None:
        inbox = FileDropDecisionInbox(tmp_path, clock=FakeClock())
        assert await inbox.poll_once("esc-1") is None
        inbox.decision_path("esc-1").write_text(
            json.dumps({"request_id": "esc-1", "verdict": "continue"})
        )
        decision = await inbox.poll_once("esc-1")
        assert decision is not None
        assert decision.verdict is EscalationVerdict.CONTINUE

    async def test_a_bad_reply_is_moved_aside_and_the_wait_continues(self, tmp_path: Path) -> None:
        inbox = FileDropDecisionInbox(tmp_path, clock=FakeClock())
        inbox.decision_path("esc-1").write_text("not json at all")
        assert await inbox.poll_once("esc-1") is None
        assert not inbox.decision_path("esc-1").exists()
        assert inbox.last_rejection is not None
        assert list(tmp_path.glob("esc-1.rejected-*.json"))

    async def test_an_advice_bearing_reply_is_moved_aside(self, tmp_path: Path) -> None:
        inbox = FileDropDecisionInbox(tmp_path, clock=FakeClock())
        inbox.decision_path("esc-1").write_text(
            json.dumps({"request_id": "esc-1", "verdict": "continue", "note": "use lightgbm"})
        )
        assert await inbox.poll_once("esc-1") is None
        assert inbox.last_rejection is not None
        assert "unsupported field" in inbox.last_rejection.error


class TestOperatorChannel:
    async def test_the_loop_suspends_until_a_decision_arrives(self, tmp_path: Path) -> None:
        inbox = FileDropDecisionInbox(tmp_path, clock=FakeClock())
        client = RecordingNtfyClient()
        polls = {"n": 0}

        async def fake_sleep(_seconds: float) -> None:
            polls["n"] += 1
            if polls["n"] == 3:
                inbox.decision_path("esc-1").write_text(
                    json.dumps({"request_id": "esc-1", "verdict": "abandon"})
                )

        channel = OperatorEscalationChannel(
            inbox,
            NtfyEscalationNotifier(client),  # type: ignore[arg-type]
            poll_interval_seconds=0.01,
            repush_interval_seconds=None,
            sleep=fake_sleep,
            clock=FakeClock(),
        )
        decision = await channel.request_decision(make_request())
        assert polls["n"] == 3  # it really waited
        assert decision.verdict is EscalationVerdict.ABANDON
        assert len(client.pushes) == 1
        assert "continue | abandon | extend_cap" in client.pushes[0]

    async def test_the_operator_is_reminded_while_the_loop_waits(self, tmp_path: Path) -> None:
        inbox = FileDropDecisionInbox(tmp_path, clock=FakeClock())
        client = RecordingNtfyClient()
        polls = {"n": 0}

        async def fake_sleep(_seconds: float) -> None:
            polls["n"] += 1
            if polls["n"] == 4:
                inbox.decision_path("esc-1").write_text(
                    json.dumps({"request_id": "esc-1", "verdict": "continue"})
                )

        channel = OperatorEscalationChannel(
            inbox,
            NtfyEscalationNotifier(client, decision_hint="run the CLI"),  # type: ignore[arg-type]
            poll_interval_seconds=1.0,
            repush_interval_seconds=2.0,
            sleep=fake_sleep,
            clock=FakeClock(),
        )
        await channel.request_decision(make_request())
        assert len(client.pushes) > 1
        assert "run the CLI" in client.pushes[0]

    async def test_a_rejected_reply_does_not_end_the_wait(self, tmp_path: Path) -> None:
        inbox = FileDropDecisionInbox(tmp_path, clock=FakeClock())
        inbox.decision_path("esc-1").write_text(
            json.dumps({"request_id": "esc-1", "verdict": "maybe"})
        )
        polls = {"n": 0}

        async def fake_sleep(_seconds: float) -> None:
            polls["n"] += 1
            if polls["n"] == 2:
                inbox.decision_path("esc-1").write_text(
                    json.dumps({"request_id": "esc-1", "verdict": "continue"})
                )

        channel = OperatorEscalationChannel(
            inbox,
            None,
            poll_interval_seconds=0.01,
            sleep=fake_sleep,
            clock=FakeClock(),
        )
        decision = await channel.request_decision(make_request())
        assert decision.verdict is EscalationVerdict.CONTINUE
        assert polls["n"] == 2

    async def test_it_works_with_no_notifier_configured(self, tmp_path: Path) -> None:
        """An unset ntfy topic disables the push, never the suspension."""
        inbox = FileDropDecisionInbox(tmp_path, clock=FakeClock())
        inbox.decision_path("esc-1").write_text(
            json.dumps({"request_id": "esc-1", "verdict": "continue"})
        )
        channel = OperatorEscalationChannel(inbox, None, poll_interval_seconds=0.01)
        assert (await channel.request_decision(make_request())).verdict is (
            EscalationVerdict.CONTINUE
        )


class TestNtfyReuse:
    def test_the_notifier_wraps_the_coordinator_client_unchanged(self) -> None:
        from turing.coordinator.alerts.ntfy_client import NtfyAlertClient

        client = NtfyAlertClient("http://example.invalid", "topic")
        notifier = NtfyEscalationNotifier(client)
        assert notifier is not None  # constructed against the real class, no subclassing
        assert NtfyAlertClient.ntfy_push.__doc__ is not None


class TestDecisionCli:
    def test_continue_writes_a_valid_decision(self, tmp_path: Path) -> None:
        assert cli_main(["--loop-dir", str(tmp_path), "esc-1", "continue"]) == 0
        body = json.loads((tmp_path / "esc-1.decision.json").read_text())
        assert decode_decision(body, expected_request_id="esc-1").verdict is (
            EscalationVerdict.CONTINUE
        )

    def test_extend_cap_requires_a_number(self, tmp_path: Path) -> None:
        assert cli_main(["--loop-dir", str(tmp_path), "esc-1", "extend_cap"]) == 2
        assert not (tmp_path / "esc-1.decision.json").exists()

    def test_extend_cap_with_numbers_is_accepted(self, tmp_path: Path) -> None:
        code = cli_main(["--loop-dir", str(tmp_path), "esc-1", "extend_cap", "--extra-steps", "50"])
        assert code == 0
        decision = decode_decision(json.loads((tmp_path / "esc-1.decision.json").read_text()))
        assert decision.cap_extension is not None
        assert decision.cap_extension.extra_steps == 50

    def test_there_is_no_advice_flag(self) -> None:
        from turing.research.loop.cli import build_parser

        options: list[str] = []
        for action in build_parser()._actions:
            options.extend(action.option_strings)
        for banned in ("--note", "--advice", "--hint", "--message"):
            assert banned not in options

    def test_an_unknown_verdict_is_refused_by_argparse(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            cli_main(["--loop-dir", str(tmp_path), "esc-1", "try_harder"])

    def test_list_shows_unanswered_requests(self, tmp_path: Path, capsys: Any) -> None:
        (tmp_path / "esc-1.request.json").write_text(json.dumps(encode_request(make_request())))
        assert cli_main(["--loop-dir", str(tmp_path), "--list"]) == 0
        out = capsys.readouterr().out
        assert "esc-1" in out
        assert "no_viable_approach" in out

        cli_main(["--loop-dir", str(tmp_path), "esc-1", "abandon"])
        assert cli_main(["--loop-dir", str(tmp_path), "--list"]) == 0
        assert "no unanswered escalations" in capsys.readouterr().out


class TestChannelFactory:
    def test_it_wires_the_real_ntfy_client_from_settings(self, tmp_path: Path) -> None:
        from turing.research.loop.escalation import build_operator_channel
        from turing.research.loop.settings import ResearchLoopSettings

        settings = ResearchLoopSettings(
            _env_file=None,
            operator_ntfy_topic="turing-research",
            coordinator_ntfy_base_url="http://surface.invalid:8090",
            research_escalation_poll_seconds=0.25,
        )
        channel = build_operator_channel(tmp_path, settings)
        assert isinstance(channel, OperatorEscalationChannel)

    async def test_an_unconfigured_topic_still_suspends_and_waits(self, tmp_path: Path) -> None:
        """No push does not mean no wait: a default verdict would erase an event."""
        from turing.research.loop.escalation import build_operator_channel
        from turing.research.loop.settings import ResearchLoopSettings

        channel = build_operator_channel(
            tmp_path, ResearchLoopSettings(_env_file=None, research_escalation_poll_seconds=0.01)
        )
        (tmp_path / "esc-1.decision.json").write_text(
            json.dumps({"request_id": "esc-1", "verdict": "abandon"})
        )
        decision = await channel.request_decision(make_request())
        assert decision.verdict is EscalationVerdict.ABANDON


class TestNonBlockingGate:
    """The same delivery in the poll shape a per-attempt driver wants."""

    async def test_raise_then_poll(self, tmp_path: Path) -> None:
        from turing.research.loop.escalation import OperatorEscalationGate

        inbox = FileDropDecisionInbox(tmp_path, clock=FakeClock())
        client = RecordingNtfyClient()
        gate = OperatorEscalationGate(inbox, NtfyEscalationNotifier(client))  # type: ignore[arg-type]

        await gate.raise_escalation(make_request())
        assert (tmp_path / "esc-1.request.json").exists()
        assert len(client.pushes) == 1
        # Nobody is awake yet: the unattended case, and the normal one.
        assert await gate.await_decision("esc-1") is None

        assert cli_main(["--loop-dir", str(tmp_path), "esc-1", "continue"]) == 0
        decision = await gate.await_decision("esc-1")
        assert decision is not None
        assert decision.verdict is EscalationVerdict.CONTINUE

    async def test_both_shapes_share_one_inbox(self, tmp_path: Path) -> None:
        from turing.research.loop.escalation import OperatorEscalationGate

        inbox = FileDropDecisionInbox(tmp_path, clock=FakeClock())
        gate = OperatorEscalationGate(inbox)
        channel = OperatorEscalationChannel(inbox, None, poll_interval_seconds=0.01)
        await gate.raise_escalation(make_request())
        cli_main(["--loop-dir", str(tmp_path), "esc-1", "abandon"])
        assert (await channel.request_decision(make_request())).verdict is (
            EscalationVerdict.ABANDON
        )
