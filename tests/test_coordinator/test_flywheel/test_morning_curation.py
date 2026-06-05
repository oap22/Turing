"""Tests for morning curation (issue #265, ADR 0009 §2 "Morning").

Accept / reject / edit over vault/inbox/** → curated-vault path transitions +
SFT candidates + MORNING_CURATION reward events. Uses a tmp vault and the
in-memory EpisodeRewardsStore.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

from turing.coordinator.episode_rewards import EpisodeRewardsStore, RewardSource
from turing.coordinator.flywheel import (
    CurationDecision,
    MorningCuration,
)
from turing.vault.committer import VaultCommitter
from turing.vault.embedder import DeterministicHashEmbedder
from turing.vault.inbox_writer import InboxDraftWriter
from turing.vault.index import VaultIndex
from turing.vault.watcher import VaultWatcher

if TYPE_CHECKING:
    from pathlib import Path

SPECIALTY = "ai-ml-generalist"
_IDENTITY = ("turing-coordinator", "coordinator@turing.local")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": _IDENTITY[0],
            "GIT_AUTHOR_EMAIL": _IDENTITY[1],
            "GIT_COMMITTER_NAME": _IDENTITY[0],
            "GIT_COMMITTER_EMAIL": _IDENTITY[1],
        },
    )
    return result.stdout.strip()


def _init_vault_repo(root: Path) -> None:
    """A git-backed vault with one empty root commit (drafts stay untracked)."""
    _git(root, "init", "-q")
    _git(root, "commit", "-q", "--allow-empty", "-m", "vault: seed")


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


# ── AC#3: commit-on-promotion (ADR 0010 §4 write-side) ──────────────────────


def test_accept_without_committer_leaves_commit_sha_none(tmp_path: Path) -> None:
    # Default (no git-backed vault) preserves the plain path-move behaviour.
    _write_draft(tmp_path)
    curator = _curator(tmp_path, EpisodeRewardsStore())
    draft = curator.list_inbox()[0]

    curator.accept(draft, episode_id="ep-1", question="Q?", recorded_at_ms=1000)

    assert curator.decisions()[0].commit_sha is None


def test_accept_with_committer_records_one_structured_commit(tmp_path: Path) -> None:
    _init_vault_repo(tmp_path)
    base = _git(tmp_path, "rev-parse", "HEAD")
    _write_draft(tmp_path)  # untracked inbox draft (flywheel path)
    committer = VaultCommitter(vault_root=tmp_path, identity=_IDENTITY)
    curator = MorningCuration(
        vault_root=tmp_path, episode_rewards=EpisodeRewardsStore(), committer=committer
    )
    draft = curator.list_inbox()[0]

    curated, _ = curator.accept(
        draft, episode_id="ep-1", question="Explain QLoRA.", recorded_at_ms=1000
    )

    record = curator.decisions()[0]
    head = _git(tmp_path, "rev-parse", "HEAD")
    # Exactly one new commit, and the record points at it.
    assert record.commit_sha == head
    assert _git(tmp_path, "rev-list", "--count", f"{base}..{head}") == "1"
    # Structured message = the reward-signal audit trail.
    message = _git(tmp_path, "log", "-1", "--format=%B")
    assert "vault: curate accept night-1/answer" in message
    assert "episode_id: ep-1" in message
    assert "reward: +1.0" in message
    # The curated note is tracked; the inbox draft never entered the tree.
    tracked = _git(tmp_path, "ls-tree", "--name-only", "-r", "HEAD").splitlines()
    curated_rel = curated.relative_to(tmp_path).as_posix()
    assert curated_rel in tracked
    assert not any(t.startswith("vault/inbox/") for t in tracked)


def test_watcher_reindexes_curated_note_from_promotion_commit(tmp_path: Path) -> None:
    # End-to-end: the write-side commit is exactly what the read-side consumes.
    _init_vault_repo(tmp_path)
    _write_draft(tmp_path)
    committer = VaultCommitter(vault_root=tmp_path, identity=_IDENTITY)
    curator = MorningCuration(
        vault_root=tmp_path, episode_rewards=EpisodeRewardsStore(), committer=committer
    )
    index = VaultIndex(embedder=DeterministicHashEmbedder())
    watcher = VaultWatcher(
        vault_root=tmp_path, index=index, last_indexed_sha=_git(tmp_path, "rev-parse", "HEAD")
    )

    curated, _ = curator.accept(
        curator.list_inbox()[0], episode_id="ep-1", question="Q?", recorded_at_ms=1000
    )
    watcher.poll_once()

    snapshot = index.snapshot()
    curated_rel = curated.relative_to(tmp_path).as_posix()
    assert curated_rel in snapshot
    assert "QLoRA quantises" in snapshot[curated_rel]
    # Nothing from the inbox leaked into the curated index.
    assert not any(path.startswith("vault/inbox/") for path in snapshot)


def test_edit_with_committer_commits_corrected_target(tmp_path: Path) -> None:
    _init_vault_repo(tmp_path)
    _write_draft(tmp_path)
    committer = VaultCommitter(vault_root=tmp_path, identity=_IDENTITY)
    curator = MorningCuration(
        vault_root=tmp_path, episode_rewards=EpisodeRewardsStore(), committer=committer
    )
    corrected = "QLoRA uses 4-bit NF4 quantisation with paged optimisers."

    curated, _ = curator.edit(
        curator.list_inbox()[0],
        episode_id="ep-3",
        question="Explain QLoRA.",
        corrected_answer=corrected,
        recorded_at_ms=3000,
    )

    record = curator.decisions()[0]
    assert record.commit_sha == _git(tmp_path, "rev-parse", "HEAD")
    assert "vault: curate edit" in _git(tmp_path, "log", "-1", "--format=%B")
    assert corrected in curated.read_text(encoding="utf-8")
