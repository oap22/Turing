"""turing-gateway: in-process FastAPI surface served from pi-alpha."""

from __future__ import annotations

from turing.gateway.app import create_app
from turing.gateway.auth import COOKIE_NAME, GatewayAuth
from turing.gateway.service import GatewayService
from turing.gateway.spa import spa_assets_path

__all__ = [
    "COOKIE_NAME",
    "GatewayAuth",
    "GatewayService",
    "create_app",
    "spa_assets_path",
]
