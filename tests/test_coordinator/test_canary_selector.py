"""CanarySelector — round-robin K=1 canary worker selection per ADR 0007.

The selector is a pure function over (specialty's worker pool,
last_canary_worker_id). Picks the next worker after the last one in
stable lexicographic order; wraps to first if at end. Single-worker
specialty returns that worker idempotently.
"""

from __future__ import annotations

import pytest

from turing.coordinator.promotion.canary_selector import (
    CanarySelector,
    EmptyFleetError,
)


def test_round_robin_cycles_through_workers() -> None:
    sel = CanarySelector()
    fleet = ("a", "b", "c")
    # First call: no prior canary, picks the lexicographically-first.
    assert sel.next(fleet=fleet, last_canary_worker_id=None) == "a"
    # After 'a', next is 'b'.
    assert sel.next(fleet=fleet, last_canary_worker_id="a") == "b"
    # After 'b', next is 'c'.
    assert sel.next(fleet=fleet, last_canary_worker_id="b") == "c"
    # After 'c', wraps to 'a'.
    assert sel.next(fleet=fleet, last_canary_worker_id="c") == "a"


def test_single_worker_specialty_is_idempotent() -> None:
    sel = CanarySelector()
    assert sel.next(fleet=("only",), last_canary_worker_id=None) == "only"
    assert sel.next(fleet=("only",), last_canary_worker_id="only") == "only"


def test_unknown_last_canary_falls_back_to_first() -> None:
    """If last_canary_worker_id refers to a worker no longer in the fleet
    (worker decommissioned), pick the lex-first as a safe restart."""
    sel = CanarySelector()
    assert sel.next(fleet=("a", "b"), last_canary_worker_id="ghost") == "a"


def test_unsorted_input_is_sorted_for_stable_order() -> None:
    """Stable lexicographic order is the contract — input order must
    not affect selection."""
    sel = CanarySelector()
    assert sel.next(fleet=("c", "a", "b"), last_canary_worker_id="a") == "b"


def test_empty_fleet_raises() -> None:
    sel = CanarySelector()
    with pytest.raises(EmptyFleetError):
        sel.next(fleet=(), last_canary_worker_id=None)
