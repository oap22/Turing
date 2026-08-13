"""Accounting invariants — the runaway brake's odometer.

The cap is the only thing between a stuck solver and an unbounded loop, and it
can only trip on numbers this module produces. Every test here pins the
*direction* of a judgement call: when the accounting is unsure, it must count
more, never less. An over-count trips the brake early and costs one attempt; an
under-count disables the brake and costs the run.
"""

from __future__ import annotations

import itertools

import pytest

from turing.research.backends import (
    CONSERVATIVE_CHARS_PER_TOKEN,
    LedgerSnapshot,
    ModelTier,
    Usage,
    UsageLedger,
    estimate_tokens,
)
from turing.research.backends.errors import BackendAccountingError
from turing.research.contracts import Cap, CapConsumption

#: Characters-per-token ratios measured against a real BPE vocabulary, by
#: payload shape. These are what the estimator has to stay under to be an
#: over-count; a single divisor tuned on the first row fails every other one.
#:
#: Ratios above 1.0 are conservative for the class: any tokenizer that packs
#: *fewer* characters into a token than these makes the estimate larger still.
MEASURED_CHARS_PER_TOKEN = {
    "english prose": 3.9,
    "numeric csv rows": 2.2,
    "cjk log lines": 1.0,
}

SAMPLES = {
    "english prose": "the verifier is frozen before the attempt begins and never edited after ",
    "numeric csv rows": "0.8123,0.7719,0.9042,1024,2048,4096,17.5,22.75,0.0001,993\n",
    "cjk log lines": "実験の結果を検証する。訓練は完了した。指標は改善している。",
}


class TestTokenEstimation:
    """The estimate must exceed the true count on every payload, not just prose.

    This is the fallback the cap runs on whenever a runtime does not report its
    own usage, and an under-count there does not fail — it lets a stuck project
    run past its budget while every number still looks reasonable.
    """

    @pytest.mark.parametrize(("shape", "chars_per_token"), MEASURED_CHARS_PER_TOKEN.items())
    def test_the_estimate_exceeds_the_measured_token_count(
        self, shape: str, chars_per_token: float
    ) -> None:
        """One divisor tuned on English is an under-count everywhere else.

        Measured against a real BPE vocabulary, a flat 3.0 divisor came out at
        0.73 of the true count on numeric CSV and 0.48 on CJK — the payloads a
        research agent reads constantly. Both are now weighted separately.
        """
        text = SAMPLES[shape] * 40
        real_tokens = len(text) / chars_per_token
        assert estimate_tokens(text) >= real_tokens, (
            f"{shape} is under-estimated: {estimate_tokens(text)} < {real_tokens:.0f}"
        )

    def test_non_ascii_costs_at_least_a_token_per_character(self) -> None:
        """The floor that makes non-Latin text safe to estimate.

        No vocabulary worth estimating against packs several CJK characters
        into one token often enough to rely on, so a token per character is the
        conservative floor rather than a guess.
        """
        text = "実験結果検証訓練指標改善"
        assert estimate_tokens(text) >= len(text)

    def test_digits_cost_more_per_character_than_prose(self) -> None:
        """Numeric payloads are the CSV/metrics case, and they tokenize densely."""
        assert estimate_tokens("1234567890" * 20) > estimate_tokens("abcdefghij" * 20)

    def test_the_prose_divisor_stays_below_real_tokenizer_ratios(self) -> None:
        assert MEASURED_CHARS_PER_TOKEN["english prose"] > CONSERVATIVE_CHARS_PER_TOKEN

    def test_rounds_up_rather_than_down(self) -> None:
        assert estimate_tokens("a") == 1
        assert estimate_tokens("abcd") == 2  # 4/2.5 rounds up, not down

    def test_empty_text_costs_nothing(self) -> None:
        assert estimate_tokens("") == 0

    def test_estimating_a_concatenation_is_never_cheaper_than_its_parts(self) -> None:
        """No payload can be made cheaper by hiding it inside a longer one."""
        parts = list(SAMPLES.values())
        assert estimate_tokens("".join(parts)) >= sum(estimate_tokens(p) for p in parts) - len(
            parts
        )

    def test_a_base64_payload_is_the_recorded_residual(self) -> None:
        """Documented, not fixed: high-entropy ASCII still under-estimates.

        Bounding it would need a divisor near 1.5, which over-charges ordinary
        prose more than two-fold and cuts the usable token cap by as much. This
        pins the shape of the gap so the trade stays a decision rather than a
        surprise: the estimate is within a factor of two of a dense-ASCII true
        count, and the step and wall-clock dimensions are unaffected by it.
        """
        blob = "aGVsbG8gd29ybGQgdGhpcyBpcyBub3QgcHJvc2UgYXQgYWxs" * 40
        dense_ascii_real = len(blob) / 1.6
        assert estimate_tokens(blob) >= dense_ascii_real / 2


class TestUsage:
    def test_total_sums_every_token_class(self) -> None:
        """Cache tokens are tokens.

        A provider that reports cache classes separately would otherwise have
        most of a long run's input silently dropped from the cap.
        """
        usage = Usage(
            input_tokens=10,
            output_tokens=20,
            cache_creation_tokens=30,
            cache_read_tokens=40,
        )
        assert usage.total_tokens == 100

    def test_negative_counts_are_rejected(self) -> None:
        with pytest.raises(BackendAccountingError):
            Usage(input_tokens=-1)

    def test_addition_accumulates_every_field(self) -> None:
        total = Usage(input_tokens=1, cache_read_tokens=2) + Usage(
            output_tokens=3, cache_creation_tokens=4
        )
        assert total == Usage(
            input_tokens=1, output_tokens=3, cache_creation_tokens=4, cache_read_tokens=2
        )

    def test_estimation_flag_is_contagious(self) -> None:
        """One estimated component makes the sum estimated.

        A total that mixes reported and inferred figures is inferred; claiming
        otherwise would hide how much of a run's cap is guesswork.
        """
        mixed = Usage(input_tokens=5) + Usage(output_tokens=5, estimated=True)
        assert mixed.estimated is True

    def test_round_trips_through_a_dict(self) -> None:
        usage = Usage(1, 2, 3, 4, estimated=True)
        assert Usage.from_dict(usage.to_dict()) == usage

    def test_malformed_records_raise_rather_than_defaulting_to_zero(self) -> None:
        with pytest.raises(BackendAccountingError):
            Usage.from_dict({"input_tokens": 1})


class TestUsageLedger:
    def test_starts_empty(self) -> None:
        ledger = UsageLedger()
        assert ledger.consumption == CapConsumption()

    def test_records_steps_tokens_and_wall_clock(self) -> None:
        ledger = UsageLedger()
        ledger.record(
            tier=ModelTier.ORCHESTRATOR,
            usage=Usage(input_tokens=100, output_tokens=50),
            wall_clock_seconds=1.5,
        )
        assert ledger.consumption == CapConsumption(steps=1, tokens=150, wall_clock_seconds=1.5)

    def test_totals_never_decrease(self) -> None:
        """The ledger is append-only.

        There is no API by which a later call can reduce a recorded total, so
        no code path can quietly hand budget back to a project that spent it.
        """
        ledger = UsageLedger()
        seen: list[CapConsumption] = []
        for _ in range(5):
            ledger.record(
                tier=ModelTier.SUBSTEP,
                usage=Usage(input_tokens=10),
                wall_clock_seconds=0.1,
            )
            seen.append(ledger.consumption)
        for earlier, later in itertools.pairwise(seen):
            assert later.steps >= earlier.steps
            assert later.tokens >= earlier.tokens
            assert later.wall_clock_seconds >= earlier.wall_clock_seconds

    def test_tracks_tiers_separately(self) -> None:
        """Per-tier spend is the evidence that routing down actually happened.

        The brief's cost argument is that mundane work moves to the cheap tier.
        Without this breakdown that claim is unfalsifiable.
        """
        ledger = UsageLedger()
        ledger.record(
            tier=ModelTier.ORCHESTRATOR, usage=Usage(input_tokens=1000), wall_clock_seconds=1.0
        )
        ledger.record(tier=ModelTier.SUBSTEP, usage=Usage(input_tokens=10), wall_clock_seconds=0.1)
        assert ledger.usage_for(ModelTier.ORCHESTRATOR).total_tokens == 1000
        assert ledger.usage_for(ModelTier.SUBSTEP).total_tokens == 10
        assert ledger.tokens == 1010

    def test_unused_tier_reports_zero_rather_than_raising(self) -> None:
        assert UsageLedger().usage_for(ModelTier.SUBSTEP) == Usage()

    def test_counts_estimated_steps_separately(self) -> None:
        ledger = UsageLedger()
        ledger.record(tier=ModelTier.SUBSTEP, usage=Usage(input_tokens=5), wall_clock_seconds=0.0)
        ledger.record(
            tier=ModelTier.SUBSTEP,
            usage=Usage(input_tokens=5, estimated=True),
            wall_clock_seconds=0.0,
        )
        assert ledger.steps == 2
        assert ledger.estimated_steps == 1

    def test_rejects_negative_records(self) -> None:
        ledger = UsageLedger()
        with pytest.raises(BackendAccountingError):
            ledger.record(tier=ModelTier.SUBSTEP, usage=Usage(), wall_clock_seconds=-1.0)
        with pytest.raises(BackendAccountingError):
            ledger.record(tier=ModelTier.SUBSTEP, usage=Usage(), wall_clock_seconds=0.0, steps=-1)
        with pytest.raises(BackendAccountingError):
            ledger.record(
                tier=ModelTier.SUBSTEP,
                usage=Usage(),
                wall_clock_seconds=0.0,
                provider_calls=-1,
            )

    def test_counts_requests_separately_from_steps(self) -> None:
        """A step is a turn; a request is a round trip. They are not the same number.

        The step count is what the cap brakes on, and a retried turn is
        deliberately one step. Recording only that leaves a turn that shipped
        three requests indistinguishable from one that shipped one, which is
        how request amplification hides.
        """
        ledger = UsageLedger()
        ledger.record(
            tier=ModelTier.ORCHESTRATOR,
            usage=Usage(input_tokens=10),
            wall_clock_seconds=0.1,
            provider_calls=3,
        )
        assert ledger.steps == 1
        assert ledger.provider_calls == 3
        assert ledger.consumption.steps == 1, "the cap brakes on steps, not requests"

    def test_consumption_feeds_a_real_cap(self) -> None:
        """The ledger's output is exactly what the cap consumes."""
        cap = Cap(max_steps=2, max_tokens=1000, max_wall_clock_seconds=60.0)
        ledger = UsageLedger()
        ledger.record(
            tier=ModelTier.ORCHESTRATOR, usage=Usage(input_tokens=400), wall_clock_seconds=1.0
        )
        assert not cap.is_exhausted(ledger.consumption)
        ledger.record(
            tier=ModelTier.ORCHESTRATOR, usage=Usage(input_tokens=400), wall_clock_seconds=1.0
        )
        assert cap.is_exhausted(ledger.consumption)


class TestSnapshotAndRestore:
    def test_snapshot_survives_a_serialisation_round_trip(self) -> None:
        """This is what makes an interruption cost the remainder, not the attempt."""
        ledger = UsageLedger()
        ledger.record(
            tier=ModelTier.ORCHESTRATOR,
            usage=Usage(input_tokens=100, output_tokens=50, cache_read_tokens=900),
            wall_clock_seconds=2.5,
        )
        ledger.record(
            tier=ModelTier.SUBSTEP,
            usage=Usage(input_tokens=7, estimated=True),
            wall_clock_seconds=0.25,
        )
        snapshot = ledger.snapshot()
        rebuilt = LedgerSnapshot.from_dict(snapshot.to_dict())
        assert rebuilt == snapshot
        assert rebuilt.consumption == ledger.consumption

    def test_restored_ledger_continues_rather_than_restarting(self) -> None:
        spent = UsageLedger()
        spent.record(
            tier=ModelTier.ORCHESTRATOR, usage=Usage(input_tokens=500), wall_clock_seconds=10.0
        )

        resumed = UsageLedger()
        resumed.restore(spent.snapshot())
        assert resumed.consumption == spent.consumption

        resumed.record(
            tier=ModelTier.ORCHESTRATOR, usage=Usage(input_tokens=100), wall_clock_seconds=1.0
        )
        assert resumed.consumption == CapConsumption(steps=2, tokens=600, wall_clock_seconds=11.0)

    def test_restore_refuses_to_discard_recorded_consumption(self) -> None:
        """Restoring over a used ledger would re-grant spent budget.

        That is the exact under-count the module exists to prevent, so it is an
        error rather than a silent overwrite.
        """
        ledger = UsageLedger()
        ledger.record(tier=ModelTier.SUBSTEP, usage=Usage(input_tokens=1), wall_clock_seconds=0.0)
        with pytest.raises(BackendAccountingError, match="already recorded"):
            ledger.restore(LedgerSnapshot(steps=0, wall_clock_seconds=0.0, per_tier={}))

    def test_snapshot_is_a_copy_not_a_live_view(self) -> None:
        ledger = UsageLedger()
        ledger.record(tier=ModelTier.SUBSTEP, usage=Usage(input_tokens=10), wall_clock_seconds=0.0)
        snapshot = ledger.snapshot()
        ledger.record(tier=ModelTier.SUBSTEP, usage=Usage(input_tokens=10), wall_clock_seconds=0.0)
        assert snapshot.consumption.tokens == 10

    def test_rejects_impossible_snapshots(self) -> None:
        with pytest.raises(BackendAccountingError):
            LedgerSnapshot(steps=1, wall_clock_seconds=0.0, per_tier={}, estimated_steps=2)
        with pytest.raises(BackendAccountingError):
            LedgerSnapshot(steps=-1, wall_clock_seconds=0.0, per_tier={})

    def test_malformed_snapshot_raises_rather_than_zeroing_the_budget(self) -> None:
        """A corrupt checkpoint must not read as 'nothing spent yet'."""
        with pytest.raises(BackendAccountingError):
            LedgerSnapshot.from_dict({"steps": 1})
        with pytest.raises(BackendAccountingError):
            LedgerSnapshot.from_dict({"steps": 1, "wall_clock_seconds": 0.0, "per_tier": None})

    def test_a_record_without_a_request_count_reads_as_one_per_step(self) -> None:
        """Absent means 'older record', not 'no requests were made'.

        Defaulting the field to zero would let a checkpoint written before it
        existed claim a run made no provider calls at all.
        """
        rebuilt = LedgerSnapshot.from_dict(
            {"steps": 4, "wall_clock_seconds": 1.0, "per_tier": {}, "estimated_steps": 0}
        )
        assert rebuilt.provider_calls == 4

    def test_the_request_count_survives_a_serialisation_round_trip(self) -> None:
        ledger = UsageLedger()
        ledger.record(
            tier=ModelTier.ORCHESTRATOR,
            usage=Usage(input_tokens=10),
            wall_clock_seconds=0.1,
            provider_calls=3,
        )
        snapshot = ledger.snapshot()
        assert LedgerSnapshot.from_dict(snapshot.to_dict()) == snapshot

        resumed = UsageLedger()
        resumed.restore(snapshot)
        assert resumed.provider_calls == 3
