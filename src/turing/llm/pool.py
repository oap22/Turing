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

The pool sits *between* the router and the local provider. Local-to-peer
failover is explicit in this pool, while the router's cloud-auth fallback
unwraps the pool to the same-machine provider unless peer fallback is
explicitly enabled.

Decision order for a request:

1. If this node has the model (or has not yet been able to check), use the
   local provider. A cold cache never sends traffic off-box.
2. Otherwise pick the peer with the model whose advertised host is set and
   whose heartbeat is fresh, preferring the lowest 1-minute load per core.
3. If a peer call fails, quarantine that endpoint briefly and try another
   eligible peer before falling back locally. The router still owns the final
   local-to-cloud fallback.

Nothing here pulls models. Placement is an operator decision made with
``scripts/fleet-models.sh``; this module only routes to where they already are.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from turing.llm.base import LLMProvider
from turing.llm.endpoints import validate_ollama_endpoint

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterable

    from turing.llm.base import LLMResponse, Message, ToolDefinition
    from turing.mesh.node import PeerInfo

logger = structlog.get_logger(__name__)

PEER_FAILURE_BACKOFF = 60.0
LOCAL_MODEL_CACHE_TTL = 60.0


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


def select_peer(
    peers: Iterable[PeerInfo],
    model: str,
    *,
    allowed_hosts: Iterable[str] = (),
    excluded_hosts: set[str] | None = None,
) -> PeerInfo | None:
    """Pick the freshest, least-loaded peer that advertises ``model``.

    Pure so it can be tested without a mesh. Peers with no advertised Ollama
    host are skipped — and every advertised host must also be in the exact
    operator allowlist passed to this function.
    """
    authorized: set[str] = set()
    for host in allowed_hosts:
        try:
            authorized.add(validate_ollama_endpoint(host, field="allowed peer endpoint"))
        except ValueError:
            # A caller-provided allowlist is not authority until each entry is
            # itself a valid endpoint.  Production config validates this at
            # startup; the pure selector remains fail-closed for other users.
            continue
    excluded = excluded_hosts or set()
    candidates = [
        p
        for p in peers
        if p.ollama_host
        and p.ollama_host in authorized
        and p.ollama_host not in excluded
        and model in p.models
        and not p.is_stale
    ]
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
        allowed_peer_hosts: Iterable[str] = (),
    ) -> None:
        self._local = local
        self._model = model
        self._factory = provider_factory
        self._allowed_peer_hosts = frozenset(
            validate_ollama_endpoint(host, field="allowed peer endpoint")
            for host in allowed_peer_hosts
        )
        # host -> provider, so a peer's HTTP client is built once, not per turn.
        self._peer_providers: dict[str, LLMProvider] = {}
        # Failed Ollama endpoints remain excluded even while their Turing
        # heartbeat is live and still advertises the last known model list.
        self._peer_failed_until: dict[str, float] = {}
        # None = not yet checked; treat as "have it" (never leave the box on a guess).
        self._local_has_model: bool | None = None
        self._local_models_checked_at: float | None = None
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

    async def refresh_local_models(self, *, force: bool = False) -> bool | None:
        """Ask the local provider what it has pulled; cache the answer.

        A provider without ``list_models`` (or one that errors) leaves the
        cache at *unknown*, which the selector treats as "serve locally".
        """
        now = time.monotonic()
        if (
            not force
            and self._local_models_checked_at is not None
            and now - self._local_models_checked_at < LOCAL_MODEL_CACHE_TTL
        ):
            return self._local_has_model
        lister = self._local
        if not hasattr(lister, "list_models"):
            self._local_models_checked_at = now
            return self._local_has_model
        try:
            models = await lister.list_models()  # type: ignore[attr-defined]
        except Exception:
            self._local_models_checked_at = now
            logger.warning("peer_pool.local_model_list_failed", exc_info=True)
            return self._local_has_model
        self._local_has_model = self._model in set(models)
        self._local_models_checked_at = now
        logger.info(
            "peer_pool.local_models",
            model=self._model,
            present=self._local_has_model,
            pulled=len(models),
        )
        return self._local_has_model

    def invalidate_local_model_cache(self) -> None:
        """Force the next request to re-check the local model inventory."""
        self._local_has_model = None
        self._local_models_checked_at = None

    async def _refresh_local_models_if_stale(self) -> None:
        if self._local_models_checked_at is None or (
            time.monotonic() - self._local_models_checked_at >= LOCAL_MODEL_CACHE_TTL
        ):
            await self.refresh_local_models()

    # ── selection ───────────────────────────────────────────────────────

    def _select_target(self) -> tuple[LLMProvider, str, str | None]:
        if self._local_has_model is not False:
            return self._local, "local", None
        now = time.monotonic()
        self._peer_failed_until = {
            host: until for host, until in self._peer_failed_until.items() if until > now
        }
        while True:
            peer = select_peer(
                self._peers_fn(),
                self._model,
                allowed_hosts=self._allowed_peer_hosts,
                excluded_hosts=set(self._peer_failed_until),
            )
            if peer is None:
                return self._local, "local:no-healthy-peer-has-model", None
            host = peer.ollama_host or ""
            reason = f"peer:{peer.name}"
            provider = self._peer_providers.get(host)
            if provider is not None:
                return provider, reason, host
            try:
                provider = self._factory(host, self._model)
            except Exception:
                # A malformed or unavailable endpoint must be quarantined and
                # allow the selector to continue to another peer/local tier.
                self._quarantine(host, reason)
                continue
            self._peer_providers[host] = provider
            return provider, reason, host

    def select(self) -> tuple[LLMProvider, str]:
        """Return ``(provider, reason)`` for the next request."""
        provider, reason, _host = self._select_target()
        return provider, reason

    def _quarantine(self, host: str, reason: str) -> None:
        self._peer_failed_until[host] = time.monotonic() + PEER_FAILURE_BACKOFF
        logger.warning(
            "peer_pool.peer_quarantined",
            target=reason,
            seconds=PEER_FAILURE_BACKOFF,
            exc_info=True,
        )

    # ── LLMProvider ─────────────────────────────────────────────────────

    async def complete(
        self,
        messages: list[Message],
        system: str = "",
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        await self._refresh_local_models_if_stale()
        provider, reason, host = self._select_target()
        while True:
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
                assert host is not None
                self._quarantine(host, reason)
                provider, reason, host = self._select_target()
                # Try another healthy peer if one exists; otherwise the
                # selector returns local and the router sees any local error.

    async def stream(
        self,
        messages: list[Message],
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        await self._refresh_local_models_if_stale()
        provider, reason, host = self._select_target()
        while True:
            logger.info("peer_pool.route", model=self._model, target=reason, stream=True)
            yielded = False
            try:
                async for delta in provider.stream(
                    messages=messages,
                    system=system,
                    max_tokens=max_tokens,
                    temperature=temperature,
                ):
                    yielded = True
                    yield delta
                return
            except Exception:
                # Once any bytes reached the caller, switching models would
                # splice two unrelated generations into one response.
                if provider is self._local:
                    raise
                assert host is not None
                self._quarantine(host, reason)
                if yielded:
                    raise
                provider, reason, host = self._select_target()

    async def health_check(self) -> bool:
        status = await self.health_status()
        return bool(status["selected"]["healthy"])

    async def health_status(self) -> dict[str, Any]:
        """Report local health separately from the route selected for serving."""
        await self._refresh_local_models_if_stale()
        try:
            local_healthy = await self._local.health_check()
        except Exception:
            local_healthy = False
        provider, reason, host = self._select_target()
        if provider is self._local:
            selected = {"target": reason, "healthy": local_healthy}
            peer = None
        else:
            try:
                peer_healthy = await provider.health_check()
            except Exception:
                peer_healthy = False
            selected = {"target": reason, "healthy": peer_healthy}
            peer = {"host": host, "healthy": peer_healthy}
        return {
            "local": {"target": "local", "healthy": local_healthy},
            "peer": peer,
            "selected": selected,
        }

    # ── introspection (gateway / logs) ──────────────────────────────────

    def describe(self) -> dict[str, Any]:
        """Where ``model`` would be served right now, for the operator UI."""
        _provider, reason = self.select()
        return {
            "model": self._model,
            "local_has_model": self._local_has_model,
            "target": reason,
            "peer_hosts": sorted(self._peer_providers),
            "quarantined_hosts": sorted(self._peer_failed_until),
            "local_model_cache_age": (
                None
                if self._local_models_checked_at is None
                else max(0.0, time.monotonic() - self._local_models_checked_at)
            ),
        }
