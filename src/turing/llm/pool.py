"""Mesh-aware local tier: borrow a peer's Ollama for models this node lacks.

Every node in the fleet runs its own Ollama, and every node pulls its own
models — a 4-node fleet with four 8 GB Jetsons cannot hold the same 7 B
model on all of them *and* keep room for anything else. So the fleet
distributes: one node holds the coder model, another the summariser, and the
mesh tells everyone who has what. Presence heartbeats (:mod:`turing.mesh.presence`)
carry each node's pulled model list plus the URL its Ollama answers on
(``ollama_advertise_host``); :class:`PeerModelPool` reads that table and, when
this node has not pulled the configured model, sends the request to the
least-loaded peer that has.

The pool sits *between* the router and the local provider, so the router's
own fallbacks (local → cloud on error, cloud → local when credentials are
missing) keep working unchanged: the pool is just a smarter "local".

Decision order for a request:

1. If this node has the model (or has not yet been able to check), use the
   local provider. A cold cache never sends traffic off-box.
2. Otherwise pick the peer with the model whose advertised host is set and
   whose heartbeat is fresh, preferring the lowest 1-minute load per core.
3. If the peer call fails, retry once locally (Ollama will pull-on-demand or
   fail clearly), and let the router take it from there.

Nothing here pulls models. Placement is an operator decision made with
``scripts/fleet-models.sh``; this module only routes to where they already are.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from turing.llm.base import LLMProvider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterable

    from turing.llm.base import LLMResponse, Message, ToolDefinition
    from turing.mesh.node import PeerInfo

logger = structlog.get_logger(__name__)


def peer_load(peer: PeerInfo) -> float:
    """1-minute load average per core, or a pessimistic 1.0 when unknown.

    Unknown load sorts *after* any measured node, so a peer that has not yet
    reported specs is only chosen when nothing better exists.
    """
    specs = peer.specs
    if specs is None:
        return 1.0
    cores = max(int(getattr(specs, "cpu_cores", 1) or 1), 1)
    return float(getattr(specs, "loadavg_1m", 0.0) or 0.0) / cores


def select_peer(peers: Iterable[PeerInfo], model: str) -> PeerInfo | None:
    """Pick the freshest, least-loaded peer that advertises ``model``.

    Pure so it can be tested without a mesh. Peers with no advertised Ollama
    host are skipped — a host of ``http://localhost:11434`` would only ever
    point back at ourselves.
    """
    candidates = [p for p in peers if p.ollama_host and model in p.models and not p.is_stale]
    if not candidates:
        return None
    return min(candidates, key=lambda p: (peer_load(p), p.name))


class PeerModelPool(LLMProvider):
    """An :class:`LLMProvider` that serves ``model`` from wherever it lives."""

    def __init__(
        self,
        local: LLMProvider,
        *,
        model: str,
        provider_factory: Callable[[str, str], LLMProvider],
    ) -> None:
        self._local = local
        self._model = model
        self._factory = provider_factory
        # host -> provider, so a peer's HTTP client is built once, not per turn.
        self._peer_providers: dict[str, LLMProvider] = {}
        # None = not yet checked; treat as "have it" (never leave the box on a guess).
        self._local_has_model: bool | None = None
        self._peers_fn: Callable[[], Iterable[PeerInfo]] = lambda: ()

    # ── wiring ──────────────────────────────────────────────────────────

    @property
    def model(self) -> str:
        return self._model

    @property
    def local(self) -> LLMProvider:
        return self._local

    def attach_peers(self, peers_fn: Callable[[], Iterable[PeerInfo]]) -> None:
        """Late-bind the peer table (the mesh boots after the LLM tier)."""
        self._peers_fn = peers_fn

    @property
    def local_has_model(self) -> bool | None:
        return self._local_has_model

    async def refresh_local_models(self) -> bool | None:
        """Ask the local provider what it has pulled; cache the answer.

        A provider without ``list_models`` (or one that errors) leaves the
        cache at *unknown*, which the selector treats as "serve locally".
        """
        lister = self._local
        if not hasattr(lister, "list_models"):
            return self._local_has_model
        try:
            models = await lister.list_models()  # type: ignore[attr-defined]
        except Exception:
            logger.warning("peer_pool.local_model_list_failed", exc_info=True)
            return self._local_has_model
        self._local_has_model = self._model in set(models)
        logger.info(
            "peer_pool.local_models",
            model=self._model,
            present=self._local_has_model,
            pulled=len(models),
        )
        return self._local_has_model

    # ── selection ───────────────────────────────────────────────────────

    def select(self) -> tuple[LLMProvider, str]:
        """Return ``(provider, reason)`` for the next request."""
        if self._local_has_model is not False:
            return self._local, "local"
        peer = select_peer(self._peers_fn(), self._model)
        if peer is None:
            return self._local, "local:no-peer-has-model"
        host = peer.ollama_host or ""
        provider = self._peer_providers.get(host)
        if provider is None:
            provider = self._factory(host, self._model)
            self._peer_providers[host] = provider
        return provider, f"peer:{peer.name}"

    # ── LLMProvider ─────────────────────────────────────────────────────

    async def complete(
        self,
        messages: list[Message],
        system: str = "",
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        provider, reason = self.select()
        logger.info("peer_pool.route", model=self._model, target=reason)
        try:
            return await provider.complete(
                messages=messages,
                system=system,
                tools=tools,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception:
            if provider is self._local:
                raise
            # A peer that just died is the common case here; its heartbeat
            # will lapse within a minute. Serve this turn locally rather
            # than fail it, and let the router's own fallback chain handle a
            # local failure on top.
            logger.warning("peer_pool.peer_failed_retrying_local", target=reason, exc_info=True)
            return await self._local.complete(
                messages=messages,
                system=system,
                tools=tools,
                max_tokens=max_tokens,
                temperature=temperature,
            )

    async def stream(
        self,
        messages: list[Message],
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        provider, reason = self.select()
        logger.info("peer_pool.route", model=self._model, target=reason, stream=True)
        async for delta in provider.stream(
            messages=messages,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
        ):
            yield delta

    async def health_check(self) -> bool:
        return await self._local.health_check()

    # ── introspection (gateway / logs) ──────────────────────────────────

    def describe(self) -> dict[str, Any]:
        """Where ``model`` would be served right now, for the operator UI."""
        _provider, reason = self.select()
        return {
            "model": self._model,
            "local_has_model": self._local_has_model,
            "target": reason,
            "peer_hosts": sorted(self._peer_providers),
        }
