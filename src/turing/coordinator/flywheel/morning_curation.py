"""Morning curation — the Phase 0 human critic (ADR 0009 §2 "Morning", #265).

Each morning the operator opens the inbox with Claude Code over SSH, reviews
every overnight draft together with its fetched sources, and **accepts /
rejects / edits** it. Those human decisions are the **Phase 0 reward signal**
(this replaces the ADR 0004 real-time critic and the ADR 0006 Discord reward UI
for Phase 0).

- **list_inbox** surfaces, per draft: the parsed frontmatter, the fetched
  source list, and the answer/reasoning split out of the body.
- **accept** promotes the draft out of ``vault/inbox/**`` into the curated tree
  (a path move) and yields a candidate **SFT pair** ``(question, reasoning,
  answer)`` — reasoning-bearing, as ADR 0009 requires.
- **edit** captures the operator's corrected answer as the SFT target and
  promotes that; **reject** discards the draft. All three are logged.
- Every decision writes a ``MORNING_CURATION`` reward event against the
  episode, so the path to autonomy has the overnight run *and* the human
  decision per episode.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from turing.coordinator.episode_rewards import write_morning_curation

if TYPE_CHECKING:
    from turing.coordinator.episode_rewards import EpisodeRewardsStore

# Default decision → reward mapping. Accept is a clean positive; reject a clean
# negative; edit is net-positive-but-imperfect (the draft was salvageable, but
# the worker's raw output still needed correction).
ACCEPT_REWARD = 1.0
REJECT_REWARD = -1.0
EDIT_REWARD = 0.3

_INBOX_PARTS = ("vault", "inbox")
_ANSWER_HEADING = "# Answer"
_REASONING_HEADING = "## Reasoning"
_SOURCES_HEADING = "## Sources"


class CurationDecision(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    EDIT = "edit"


@dataclass(frozen=True)
class InboxDraft:
    """A parsed overnight draft awaiting morning review."""

    path: Path
    task_id: str
    specialty: str
    frontmatter: dict[str, str]
    sources: list[dict[str, str]]
    answer: str
    reasoning: str
    body: str


@dataclass(frozen=True)
class SFTCandidate:
    """A reasoning-bearing candidate training pair from an accepted/edited draft."""

    question: str
    reasoning: str
    answer: str
    specialty: str


@dataclass(frozen=True)
class CurationRecord:
    """The logged record of one curation decision."""

    decision: CurationDecision
    task_id: str
    episode_id: str
    recorded_at_ms: int
    curated_path: Path | None = None
    corrected_answer: str | None = None


class MorningCuration:
    """Drives accept / reject / edit over ``vault/inbox/**``."""

    def __init__(
        self,
        *,
        vault_root: Path,
        episode_rewards: EpisodeRewardsStore,
        curated_subdir: str = "curated",
        accept_reward: float = ACCEPT_REWARD,
        reject_reward: float = REJECT_REWARD,
        edit_reward: float = EDIT_REWARD,
    ) -> None:
        self._root = Path(vault_root).resolve()
        self._rewards = episode_rewards
        self._curated_subdir = curated_subdir
        self._accept_reward = accept_reward
        self._reject_reward = reject_reward
        self._edit_reward = edit_reward
        self._decisions: list[CurationRecord] = []
        self._candidates: list[SFTCandidate] = []

    @property
    def inbox_root(self) -> Path:
        return self._root.joinpath(*_INBOX_PARTS)

    # ── review ────────────────────────────────────────────────────────────

    def list_inbox(self) -> list[InboxDraft]:
        """Every inbox draft, parsed, sorted by path for stable review order."""
        root = self.inbox_root
        if not root.exists():
            return []
        return [parse_inbox_draft(p) for p in sorted(root.rglob("*.md"))]

    # ── decisions ───────────────────────────────────────────────────────────

    def accept(
        self,
        draft: InboxDraft,
        *,
        episode_id: str,
        question: str,
        recorded_at_ms: int,
    ) -> tuple[Path, SFTCandidate]:
        """Promote the draft to the curated tree; reward + record an SFT pair."""
        curated = self._promote(draft, body=draft.body)
        candidate = SFTCandidate(
            question=question,
            reasoning=draft.reasoning,
            answer=draft.answer,
            specialty=draft.specialty,
        )
        self._candidates.append(candidate)
        self._reward(episode_id, self._accept_reward, recorded_at_ms)
        self._log(
            CurationDecision.ACCEPT,
            draft,
            episode_id=episode_id,
            recorded_at_ms=recorded_at_ms,
            curated_path=curated,
        )
        return curated, candidate

    def edit(
        self,
        draft: InboxDraft,
        *,
        episode_id: str,
        question: str,
        corrected_answer: str,
        recorded_at_ms: int,
    ) -> tuple[Path, SFTCandidate]:
        """Capture the operator's corrected answer as the SFT target, promote it."""
        corrected_body = _replace_answer(draft.body, corrected_answer)
        curated = self._promote(draft, body=corrected_body)
        candidate = SFTCandidate(
            question=question,
            reasoning=draft.reasoning,
            answer=corrected_answer,  # the corrected target, not the raw draft
            specialty=draft.specialty,
        )
        self._candidates.append(candidate)
        self._reward(episode_id, self._edit_reward, recorded_at_ms)
        self._log(
            CurationDecision.EDIT,
            draft,
            episode_id=episode_id,
            recorded_at_ms=recorded_at_ms,
            curated_path=curated,
            corrected_answer=corrected_answer,
        )
        return curated, candidate

    def reject(
        self,
        draft: InboxDraft,
        *,
        episode_id: str,
        recorded_at_ms: int,
    ) -> None:
        """Discard the draft (removed from the inbox); reward + record."""
        draft.path.unlink(missing_ok=True)
        self._reward(episode_id, self._reject_reward, recorded_at_ms)
        self._log(
            CurationDecision.REJECT,
            draft,
            episode_id=episode_id,
            recorded_at_ms=recorded_at_ms,
        )

    # ── accessors ────────────────────────────────────────────────────────────

    def decisions(self) -> list[CurationRecord]:
        return list(self._decisions)

    def sft_candidates(self) -> list[SFTCandidate]:
        return list(self._candidates)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _promote(self, draft: InboxDraft, *, body: str) -> Path:
        """Move the draft out of vault/inbox/** into the curated tree."""
        curated_dir = self._root / self._curated_subdir / draft.specialty
        curated_dir.mkdir(parents=True, exist_ok=True)
        curated_path = curated_dir / draft.path.name
        if body == draft.body:
            shutil.move(str(draft.path), str(curated_path))
        else:
            # Edited: write the corrected content to curated, drop the inbox copy.
            header = _frontmatter_block(draft.path.read_text(encoding="utf-8"))
            curated_path.write_text(header + body, encoding="utf-8")
            draft.path.unlink(missing_ok=True)
        return curated_path

    def _reward(self, episode_id: str, value: float, recorded_at_ms: int) -> None:
        write_morning_curation(
            self._rewards,
            episode_id=episode_id,
            value=value,
            recorded_at_ms=recorded_at_ms,
        )

    def _log(
        self,
        decision: CurationDecision,
        draft: InboxDraft,
        *,
        episode_id: str,
        recorded_at_ms: int,
        curated_path: Path | None = None,
        corrected_answer: str | None = None,
    ) -> None:
        self._decisions.append(
            CurationRecord(
                decision=decision,
                task_id=draft.task_id,
                episode_id=episode_id,
                recorded_at_ms=recorded_at_ms,
                curated_path=curated_path,
                corrected_answer=corrected_answer,
            )
        )


# ── parsing ────────────────────────────────────────────────────────────────


def parse_inbox_draft(path: Path) -> InboxDraft:
    """Parse a draft written by ``InboxDraftWriter`` into an :class:`InboxDraft`."""
    text = path.read_text(encoding="utf-8")
    frontmatter, sources, body = _split_frontmatter(text)
    return InboxDraft(
        path=path,
        task_id=frontmatter.get("task_id", ""),
        specialty=frontmatter.get("specialty", ""),
        frontmatter=frontmatter,
        sources=sources,
        answer=_section(body, _ANSWER_HEADING, _REASONING_HEADING),
        reasoning=_section(body, _REASONING_HEADING, _SOURCES_HEADING),
        body=body,
    )


def _split_frontmatter(text: str) -> tuple[dict[str, str], list[dict[str, str]], str]:
    """Return ``(scalar_frontmatter, sources_list, body)``."""
    if not text.startswith("---"):
        return {}, [], text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, [], text
    header = text[3:end].strip("\n")
    body = text[end + len("\n---") :].lstrip("\n")

    scalars: dict[str, str] = {}
    sources: list[dict[str, str]] = []
    in_sources = False
    current: dict[str, str] = {}
    for raw_line in header.splitlines():
        if raw_line.strip() == "sources:":
            in_sources = True
            continue
        if in_sources:
            stripped = raw_line.strip()
            if stripped.startswith("- "):
                if current:
                    sources.append(current)
                current = {}
                stripped = stripped[2:]
            if ":" in stripped:
                key, _, value = stripped.partition(":")
                current[key.strip()] = value.strip().strip('"')
            continue
        if ":" in raw_line:
            key, _, value = raw_line.partition(":")
            scalars[key.strip()] = value.strip()
    if current:
        sources.append(current)
    return scalars, sources, body


def _section(body: str, heading: str, next_heading: str) -> str:
    start = body.find(heading)
    if start == -1:
        return ""
    start += len(heading)
    end = body.find(next_heading, start)
    chunk = body[start:] if end == -1 else body[start:end]
    return chunk.strip()


def _frontmatter_block(text: str) -> str:
    """Return the leading ``---\\n...\\n---\\n`` block (inclusive), or ''."""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end == -1:
        return ""
    return text[: end + len("\n---")] + "\n\n"


def _replace_answer(body: str, corrected_answer: str) -> str:
    """Swap the ``# Answer`` section's content for the operator's correction."""
    start = body.find(_ANSWER_HEADING)
    if start == -1:
        return f"{_ANSWER_HEADING}\n\n{corrected_answer}\n\n{body}"
    rest_start = start + len(_ANSWER_HEADING)
    next_heading = body.find(_REASONING_HEADING, rest_start)
    tail = body[next_heading:] if next_heading != -1 else ""
    return f"{_ANSWER_HEADING}\n\n{corrected_answer}\n\n{tail}"
