"""Tests for morning curation (issue #265, ADR 0009 §2 "Morning").

Accept / reject / edit over vault/inbox/** → curated-vault path transitions +
SFT candidates + MORNING_CURATION reward events. Uses a tmp vault and the
in-memory EpisodeRewardsStore.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from turing.coordinator.episode_rewards import EpisodeRewardsStore, RewardSource
from turing.coordinator.flywheel import (
    CurationDecision,
    MorningCuration,
)
from turing.vault.inbox_writer import InboxDraftWriter

if TYPE_CHECKING:
    from pathlib import Path

SPECIALTY = "ai-ml-generalist"


def _write_draft(
    tmp_path: Path,
    *,
    task_id: str = "night-1",
    slug: str = "answer",
    answer: str = "QLoRA quantises the base then trains adapters.",
    reasoning: str = "1. Fetched the paper.\n2. Summarised the method.",
) -> Path:
    writer = InboxDraftWriter(vault_root=tmp_path)
    sources = [{"id": "src-1", "url": "https://arxiv.org/abs/2305.14314", "title": "QLoRA"}]
    body = (
        f"# Answer\n\n{answer}\n\n"
        f"## Reasoning\n\n{reasoning}\n\n"
        f"## Sources\n\n- [src-1] QLoRA — https://arxiv.org/abs/2305.14314"
    )
    fm = {
        "source": "w",
        "task_id": task_id,
        "specialty": SPECIALTY,
        "confidence": 0.7,
        "critic_score": 0.0,
    }
    return writer.write(frontmatter=fm, sources=sources, body=body, slug=slug)


def _curator(tmp_path: Path, rewards: EpisodeRewardsStore) -> MorningCuration:
    return MorningCuration(vault_root=tmp_path, episode_rewards=rewards)


# ── review ───────────────────────────────────────────────────────────────────


def test_list_inbox_surfaces_drafts_with_sources(tmp_path: Path) -> None:
    _write_draft(tmp_path)
    curator = _curator(tmp_path, EpisodeRewardsStore())

    drafts = curator.list_inbox()

    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.task_id == "night-1"
    assert draft.specialty == SPECIALTY
    assert len(draft.sources) == 1
    assert draft.sources[0]["url"] == "https://arxiv.org/abs/2305.14314"
    assert "QLoRA quantises" in draft.answer
    assert "Fetched the paper" in draft.reasoning


# ── accept ───────────────────────────────────────────────────────────────────


def test_accept_promotes_creates_sft_candidate_and_rewards(tmp_path: Path) -> None:
    inbox_path = _write_draft(tmp_path)
    rewards = EpisodeRewardsStore()
    curator = _curator(tmp_path, rewards)
    draft = curator.list_inbox()[0]

    curated, candidate = curator.accept(
        draft, episode_id="ep-1", question="Explain QLoRA.", recorded_at_ms=1000
    )

    # Path moved out of vault/inbox/** into the curated tree.
    assert not inbox_path.exists()
    assert curated.exists()
    assert not curated.resolve().is_relative_to((tmp_path / "vault" / "inbox").resolve())
    # SFT candidate carries the reasoning, not just the answer.
    assert candidate.question == "Explain QLoRA."
    assert "QLoRA quantises" in candidate.answer
    assert "Fetched the paper" in candidate.reasoning
    # Reward event written against the episode.
    assert rewards.effective_reward("ep-1") == 1.0
    events = rewards.events_for("ep-1")
    assert events[0].source is RewardSource.MORNING_CURATION
    assert curator.sft_candidates() == [candidate]


# ── reject ───────────────────────────────────────────────────────────────────


def test_reject_discards_draft_and_writes_negative_reward(tmp_path: Path) -> None:
    inbox_path = _write_draft(tmp_path)
    rewards = EpisodeRewardsStore()
    curator = _curator(tmp_path, rewards)
    draft = curator.list_inbox()[0]

    curator.reject(draft, episode_id="ep-2", recorded_at_ms=2000)

    assert not inbox_path.exists()  # discarded from the inbox
    assert rewards.effective_reward("ep-2") == -1.0
    assert curator.sft_candidates() == []  # rejects are not training pairs
    record = curator.decisions()[0]
    assert record.decision is CurationDecision.REJECT
    assert record.episode_id == "ep-2"


# ── edit ─────────────────────────────────────────────────────────────────────


def test_edit_captures_corrected_target_and_rewards(tmp_path: Path) -> None:
    inbox_path = _write_draft(tmp_path)
    rewards = EpisodeRewardsStore()
    curator = _curator(tmp_path, rewards)
    draft = curator.list_inbox()[0]

    corrected = "QLoRA uses 4-bit NF4 quantisation with paged optimisers."
    curated, candidate = curator.edit(
        draft,
        episode_id="ep-3",
        question="Explain QLoRA.",
        corrected_answer=corrected,
        recorded_at_ms=3000,
    )

    assert not inbox_path.exists()
    # The corrected answer is the SFT target, not the raw draft.
    assert candidate.answer == corrected
    assert "Fetched the paper" in candidate.reasoning  # reasoning retained
    assert corrected in curated.read_text(encoding="utf-8")
    # Edit reward is partial credit.
    assert rewards.effective_reward("ep-3") == 0.3
    record = curator.decisions()[0]
    assert record.decision is CurationDecision.EDIT
    assert record.corrected_answer == corrected


def test_empty_inbox_lists_nothing(tmp_path: Path) -> None:
    curator = _curator(tmp_path, EpisodeRewardsStore())
    assert curator.list_inbox() == []
