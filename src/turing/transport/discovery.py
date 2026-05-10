"""Pyre/Zyre announcement payload — discovery-only role.

ADR 0001 reduces Pyre/Zyre to discovery: announce the coordinator's NATS URL
on the LAN and nothing else. The legacy `mesh.discovery` module advertises
node capabilities, which is now handled by NATS-side capability registration.

This module owns the announce-payload contract independent of Pyre itself, so
the contract can be unit-tested without a live Zyre node.
"""

from __future__ import annotations

from dataclasses import dataclass

_NATS_URL_HEADER = "nats_url"


@dataclass(frozen=True)
class NatsUrlAnnouncement:
    nats_url: str

    def to_headers(self) -> dict[str, str]:
        return {_NATS_URL_HEADER: self.nats_url}

    @classmethod
    def from_headers(cls, headers: dict[str, str]) -> NatsUrlAnnouncement:
        url = headers.get(_NATS_URL_HEADER)
        if not url:
            raise ValueError(f"announcement missing required header {_NATS_URL_HEADER!r}")
        return cls(nats_url=url)
