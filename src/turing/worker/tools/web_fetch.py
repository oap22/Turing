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
from urllib.parse import urljoin, urlparse

if TYPE_CHECKING:
    from collections.abc import Iterable

# A fetcher resolves a URL to ``(title, text)``. Injected so tests can stub it
# and so the heavyweight HTTP path stays out of the import graph.
Fetcher = Callable[[str], Awaitable[tuple[str, str]]]

# Worker-side fetches are deliberately bounded so a runaway model cannot pull
# a huge page into the prompt window.
MAX_FETCH_CHARS = 8_000

# Canonical research-grounding hosts — the default for
# ``TuringConfig.web_fetch_allowed_hosts`` (env ``TURING_WEB_FETCH_ALLOWED_HOSTS``).
# Subdomains of each entry are allowed, so ``scholar.google.com`` admits Google
# Scholar (paper search, citations, PDF links) without ever allowlisting
# ``google.com`` itself. Redirects are not a way around the list — the
# production fetcher re-checks every hop (see :func:`httpx_fetcher`), which is
# what keeps Scholar's own ``/scholar_url?url=…`` open redirect from becoming a
# bypass. Generic redirectors (doi.org and friends) still stay off the list:
# they resolve to arbitrary publisher hosts that aren't allowlisted, so
# admitting them buys nothing.
DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = (
    "arxiv.org",
    "scholar.google.com",
    "semanticscholar.org",
    "openreview.net",
    "aclanthology.org",
    "paperswithcode.com",
    "openalex.org",
    "dblp.org",
    "wikipedia.org",
    "huggingface.co",
)


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


# Redirect chains beyond this are refused — research sources resolve in a
# hop or two; anything longer is a loop or a laundering chain.
MAX_REDIRECT_HOPS = 5


def httpx_fetcher(
    client: object, *, allowlist: WebFetchAllowlist, timeout_s: float = 20.0
) -> Fetcher:
    """Build a :data:`Fetcher` over an ``httpx.AsyncClient``-like object.

    Redirects are followed *manually* and every hop is re-checked against the
    allowlist: an allowed host that 3xx-redirects elsewhere (Google Scholar's
    ``/scholar_url?url=…`` open redirect, say) must not become an allowlist
    bypass to arbitrary or internal hosts. Kept import-light (stdlib +
    duck-typed client only).
    """

    async def _fetch(url: str) -> tuple[str, str]:
        current = url
        for _ in range(MAX_REDIRECT_HOPS + 1):
            allowlist.check(current)
            response = await client.get(  # type: ignore[attr-defined]
                current, timeout=timeout_s, follow_redirects=False
            )
            location = response.headers.get("location", "")
            if 300 <= response.status_code < 400 and location:
                current = urljoin(current, location)
                continue
            break
        else:
            raise WebFetchNotAllowedError(
                f"web_fetch refused {url!r}: more than {MAX_REDIRECT_HOPS} redirects"
            )
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
