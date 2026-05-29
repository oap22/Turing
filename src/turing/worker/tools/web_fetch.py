"""``web_fetch`` — allowlist-gated external fetch, executed locally on workers.

ADR 0009 §2 makes grounding load-bearing: a worker answers a research question
by **fetching real external sources** rather than trusting the small model's
unaided output ("knowledge must enter from outside the student model"). This is
a low-risk, worker-local tool (it never touches the coordinator shell gate),
restricted to an **allowlist of hosts** so a model can't be talked into
fetching arbitrary URLs.

The network call itself is injected (``Fetcher``) so the grounding loop is
testable without real HTTP; :func:`httpx_fetcher` wraps the project's ``httpx``
dependency for production. Fetched content is *untrusted* — callers wrap it in
``<untrusted_data>`` before it re-enters a prompt.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from collections.abc import Iterable

# A fetcher resolves a URL to ``(title, text)``. Injected so tests can stub it
# and so the heavyweight HTTP path stays out of the import graph.
Fetcher = Callable[[str], Awaitable[tuple[str, str]]]

# Worker-side fetches are deliberately bounded so a runaway model cannot pull
# a huge page into the prompt window.
MAX_FETCH_CHARS = 8_000


class WebFetchNotAllowedError(RuntimeError):
    """Raised when a URL's host is not on the allowlist or the scheme is unsafe."""


@dataclass(frozen=True)
class FetchedSource:
    """One external source a worker grounded its answer in (provenance)."""

    id: str
    url: str
    title: str
    text: str
    fetched_at_ms: int

    def to_frontmatter(self) -> dict[str, str]:
        """Compact dict for the draft's ``sources:`` frontmatter list."""
        return {"id": self.id, "url": self.url, "title": self.title}


class WebFetchAllowlist:
    """Host allowlist. A host matches if it equals an allowed entry or is a
    subdomain of one (``arxiv.org`` allows ``export.arxiv.org``)."""

    def __init__(self, allowed_hosts: Iterable[str]) -> None:
        self._hosts = frozenset(h.lower().lstrip(".") for h in allowed_hosts if h.strip())

    def is_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        return any(host == allowed or host.endswith("." + allowed) for allowed in self._hosts)

    def check(self, url: str) -> None:
        if not self.is_allowed(url):
            raise WebFetchNotAllowedError(
                f"web_fetch refused {url!r}: host not on the allowlist or unsafe scheme"
            )


async def web_fetch(
    url: str,
    *,
    allowlist: WebFetchAllowlist,
    fetcher: Fetcher,
    now_ms: int,
    source_id: str,
) -> FetchedSource:
    """Fetch ``url`` if the allowlist permits it; return a :class:`FetchedSource`.

    Raises :class:`WebFetchNotAllowedError` for a disallowed host / unsafe
    scheme — the caller surfaces that back to the model as a tool error rather
    than crashing the loop.
    """
    allowlist.check(url)
    title, text = await fetcher(url)
    if len(text) > MAX_FETCH_CHARS:
        text = text[:MAX_FETCH_CHARS]
    return FetchedSource(
        id=source_id,
        url=url,
        title=title,
        text=text,
        fetched_at_ms=now_ms,
    )


def httpx_fetcher(client: object, *, timeout_s: float = 20.0) -> Fetcher:  # pragma: no cover
    """Build a :data:`Fetcher` over an ``httpx.AsyncClient``-like object.

    Kept import-light and untested here (no live network in CI); the grounding
    loop is exercised against an injected stub fetcher instead.
    """

    async def _fetch(url: str) -> tuple[str, str]:
        response = await client.get(url, timeout=timeout_s, follow_redirects=True)  # type: ignore[attr-defined]
        response.raise_for_status()
        text = response.text
        # Best-effort <title> extraction without an HTML parser dependency.
        title = url
        lowered = text.lower()
        start = lowered.find("<title>")
        if start != -1:
            end = lowered.find("</title>", start)
            if end != -1:
                title = text[start + len("<title>") : end].strip() or url
        return title, text

    return _fetch
