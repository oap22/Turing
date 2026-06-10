"""Tests for grounded research (issue #262, ADR 0009 §2 "Night").

Exercises the grounded Think→Act loop with a scripted LLM + stub fetcher (no
GPU, no live HTTP) and a tmp vault, asserting: answers are grounded in fetched
sources, the draft lands in vault/inbox with frontmatter + the source list,
writes are confined to vault/inbox/**, and the episode captures the reasoning
trajectory (not just the final answer).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from turing.coordinator.lifecycle.episode_store import EpisodeStore
from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.llm.base import LLMResponse, ToolCall
from turing.vault.inbox_writer import InboxDraftWriter
from turing.vault.proposer import InvalidProposalPathError
from turing.worker.grounding import (
    GroundedResearcher,
    build_grounded_episode,
)
from turing.worker.tools.web_fetch import (
    DEFAULT_ALLOWED_HOSTS,
    MAX_REDIRECT_HOPS,
    WebFetchAllowlist,
    WebFetchNotAllowedError,
    httpx_fetcher,
    web_fetch,
)

if TYPE_CHECKING:
    from pathlib import Path

SPECIALTY = "ai-ml-generalist"


class ScriptedLLM:
    """Returns canned LLMResponses in order (last repeats if over-consumed)."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self._i = 0

    async def complete(self, messages, system="", tools=None, max_tokens=4096, temperature=0.7):
        resp = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return resp


async def _fake_fetcher(url: str) -> tuple[str, str]:
    return (f"Title for {url}", f"Real source text from {url} explaining LoRA fine-tuning.")


def _researcher(tmp_path: Path, *, responses: list[LLMResponse], allowed=("arxiv.org",)):
    return GroundedResearcher(
        llm=ScriptedLLM(responses),
        allowlist=WebFetchAllowlist(allowed),
        fetcher=_fake_fetcher,
        writer=InboxDraftWriter(vault_root=tmp_path),
        worker_id="jetson-1",
        now_ms=1000,
    )


def _fetch_then_answer() -> list[LLMResponse]:
    return [
        LLMResponse(
            content="I should ground this in a real paper.",
            tool_calls=[
                ToolCall(
                    id="t1", name="web_fetch", arguments={"url": "https://arxiv.org/abs/2305.14314"}
                )
            ],
            model="qwen2.5:7b",
            usage={"total_tokens": 50},
        ),
        LLMResponse(
            content="QLoRA fine-tunes a quantised base with low-rank adapters.",
            model="qwen2.5:7b",
            usage={"total_tokens": 40},
        ),
    ]


# ── allowlist unit ───────────────────────────────────────────────────────────


def test_allowlist_matches_host_and_subdomain() -> None:
    allow = WebFetchAllowlist(["arxiv.org", "wikipedia.org"])
    assert allow.is_allowed("https://arxiv.org/abs/1234")
    assert allow.is_allowed("https://export.arxiv.org/abs/1234")  # subdomain
    assert not allow.is_allowed("https://evil.com/x")
    assert not allow.is_allowed("ftp://arxiv.org/x")  # unsafe scheme


def test_default_hosts_admit_research_paper_sources() -> None:
    """The canonical defaults must give workers the open web for papers —
    Google Scholar in particular (goal of the grounding loop, ADR 0009 §2)."""
    allow = WebFetchAllowlist(DEFAULT_ALLOWED_HOSTS)
    assert allow.is_allowed("https://scholar.google.com/scholar?q=lora+fine-tuning")
    assert allow.is_allowed("https://arxiv.org/abs/2305.14314")
    assert allow.is_allowed("https://export.arxiv.org/abs/2305.14314")
    assert allow.is_allowed("https://www.semanticscholar.org/paper/abc")
    assert allow.is_allowed("https://api.semanticscholar.org/graph/v1/paper/search?query=qlora")
    assert allow.is_allowed("https://openreview.net/forum?id=xyz")
    assert allow.is_allowed("https://aclanthology.org/2023.acl-long.1/")
    assert allow.is_allowed("https://en.wikipedia.org/wiki/Low-rank_adaptation")


def test_default_hosts_do_not_leak_beyond_scholar() -> None:
    """scholar.google.com must not admit the rest of google.com (or lookalikes)."""
    allow = WebFetchAllowlist(DEFAULT_ALLOWED_HOSTS)
    assert not allow.is_allowed("https://www.google.com/search?q=papers")
    assert not allow.is_allowed("https://mail.google.com/")
    assert not allow.is_allowed("https://scholar.google.com.evil.com/x")  # suffix spoof
    assert not allow.is_allowed("https://notarxiv.org/abs/1")


@pytest.mark.asyncio
async def test_web_fetch_allows_google_scholar_with_defaults() -> None:
    source = await web_fetch(
        "https://scholar.google.com/scholar?q=qlora",
        allowlist=WebFetchAllowlist(DEFAULT_ALLOWED_HOSTS),
        fetcher=_fake_fetcher,
        now_ms=1,
        source_id="s1",
    )
    assert source.url == "https://scholar.google.com/scholar?q=qlora"
    assert source.title


@pytest.mark.asyncio
async def test_web_fetch_refuses_disallowed_host() -> None:
    allow = WebFetchAllowlist(["arxiv.org"])
    with pytest.raises(WebFetchNotAllowedError):
        await web_fetch(
            "https://evil.com/x", allowlist=allow, fetcher=_fake_fetcher, now_ms=1, source_id="s1"
        )


# ── httpx fetcher redirect validation ────────────────────────────────────────


class _StubResponse:
    def __init__(self, status_code: int, *, location: str | None = None, text: str = "") -> None:
        self.status_code = status_code
        self.headers = {"location": location} if location is not None else {}
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _StubClient:
    """httpx.AsyncClient-like: serves a scripted URL → response map."""

    def __init__(self, routes: dict[str, _StubResponse]) -> None:
        self._routes = routes
        self.requested: list[str] = []

    async def get(self, url: str, *, timeout: float, follow_redirects: bool) -> _StubResponse:
        assert follow_redirects is False  # hops must be validated, never auto-followed
        self.requested.append(url)
        return self._routes[url]


@pytest.mark.asyncio
async def test_httpx_fetcher_refuses_redirect_to_disallowed_host() -> None:
    """Google Scholar's /scholar_url open redirect must not become an
    allowlist bypass: the redirect target is re-checked and refused."""
    start = "https://scholar.google.com/scholar_url?url=http://169.254.169.254/"
    client = _StubClient(
        {start: _StubResponse(302, location="http://169.254.169.254/latest/meta-data/")}
    )
    fetch = httpx_fetcher(client, allowlist=WebFetchAllowlist(DEFAULT_ALLOWED_HOSTS))
    with pytest.raises(WebFetchNotAllowedError):
        await fetch(start)
    # The internal host was never contacted.
    assert client.requested == [start]


@pytest.mark.asyncio
async def test_httpx_fetcher_follows_allowed_redirects_including_relative() -> None:
    client = _StubClient(
        {
            "https://arxiv.org/abs/2305.14314": _StubResponse(301, location="/pdf/2305.14314"),
            "https://arxiv.org/pdf/2305.14314": _StubResponse(
                302, location="https://export.arxiv.org/pdf/2305.14314"
            ),
            "https://export.arxiv.org/pdf/2305.14314": _StubResponse(
                200, text="<title>QLoRA</title>paper text"
            ),
        }
    )
    fetch = httpx_fetcher(client, allowlist=WebFetchAllowlist(DEFAULT_ALLOWED_HOSTS))
    title, text = await fetch("https://arxiv.org/abs/2305.14314")
    assert title == "QLoRA"
    assert "paper text" in text
    assert len(client.requested) == 3


@pytest.mark.asyncio
async def test_httpx_fetcher_caps_redirect_hops() -> None:
    loop_url = "https://arxiv.org/loop"
    client = _StubClient({loop_url: _StubResponse(302, location=loop_url)})
    fetch = httpx_fetcher(client, allowlist=WebFetchAllowlist(DEFAULT_ALLOWED_HOSTS))
    with pytest.raises(WebFetchNotAllowedError, match="redirects"):
        await fetch(loop_url)
    assert len(client.requested) == MAX_REDIRECT_HOPS + 1


# ── grounded draft path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_grounds_in_fetched_sources_and_writes_draft(tmp_path: Path) -> None:
    researcher = _researcher(tmp_path, responses=_fetch_then_answer())

    draft = await researcher.research(
        task_id="night-1", prompt="Explain QLoRA.", specialty=SPECIALTY, slug="qlora"
    )

    assert draft.is_grounded
    assert len(draft.sources) == 1
    assert draft.sources[0].url == "https://arxiv.org/abs/2305.14314"
    # Draft landed under vault/inbox/<task_id>/<slug>.md
    inbox = tmp_path / "vault" / "inbox" / "night-1"
    assert draft.draft_path == inbox / "qlora.md"
    text = draft.draft_path.read_text(encoding="utf-8")
    assert "task_id: night-1" in text
    assert f"specialty: {SPECIALTY}" in text
    assert "sources:" in text
    assert "url: https://arxiv.org/abs/2305.14314" in text
    assert "QLoRA fine-tunes" in text  # the grounded answer is in the body


@pytest.mark.asyncio
async def test_episode_captures_reasoning_trajectory(tmp_path: Path) -> None:
    researcher = _researcher(tmp_path, responses=_fetch_then_answer())
    draft = await researcher.research(
        task_id="night-1", prompt="Explain QLoRA.", specialty=SPECIALTY, slug="qlora"
    )

    episode = build_grounded_episode(
        draft, subtask_id="q1", worker_id="jetson-1", latency_ms=12, recorded_at_ms=2000
    )

    # The trajectory is the reasoning steps, NOT just the final answer.
    assert "I should ground this in a real paper." in episode.trajectory
    assert episode.output_text == "QLoRA fine-tunes a quantised base with low-rank adapters."
    assert episode.trajectory != (episode.output_text,)
    assert episode.outcome is SubtaskState.COMPLETED

    # And it records into an in-memory store like any other episode.
    store = EpisodeStore()
    store.record(episode)
    assert store.get("q1").trajectory == draft.reasoning


@pytest.mark.asyncio
async def test_disallowed_fetch_surfaces_error_without_crashing(tmp_path: Path) -> None:
    """A model fetching a non-allowlisted URL gets a tool error, not a crash;
    the loop continues and produces an (ungrounded, low-confidence) answer."""
    responses = [
        LLMResponse(
            content="Trying a source.",
            tool_calls=[
                ToolCall(id="t1", name="web_fetch", arguments={"url": "https://evil.com/x"})
            ],
            model="m",
            usage={"total_tokens": 10},
        ),
        LLMResponse(content="Best-effort answer without sources.", model="m"),
    ]
    researcher = _researcher(tmp_path, responses=responses)

    draft = await researcher.research(
        task_id="night-2", prompt="q", specialty=SPECIALTY, slug="ans"
    )

    assert not draft.is_grounded
    assert draft.sources == ()
    assert draft.confidence == pytest.approx(0.1)
    assert draft.draft_path.exists()


def test_writes_confined_to_inbox(tmp_path: Path) -> None:
    writer = InboxDraftWriter(vault_root=tmp_path)
    fm = {
        "source": "w",
        "task_id": "../../etc",
        "specialty": SPECIALTY,
        "confidence": 0.5,
        "critic_score": 0.0,
    }
    with pytest.raises(InvalidProposalPathError):
        writer.write(frontmatter=fm, sources=[], body="x", slug="ok")

    fm_ok = {**fm, "task_id": "night-3"}
    with pytest.raises(InvalidProposalPathError):
        writer.write(frontmatter=fm_ok, sources=[], body="x", slug="../escape")

    path = writer.write(frontmatter=fm_ok, sources=[], body="x", slug="ok")
    assert path.resolve().is_relative_to((tmp_path / "vault" / "inbox").resolve())
