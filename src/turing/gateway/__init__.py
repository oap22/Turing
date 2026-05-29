"""turing-gateway: in-process FastAPI surface served from pi-alpha."""

from __future__ import annotations

from turing.gateway.app import create_app
from turing.gateway.auth import COOKIE_NAME, GatewayAuth
from turing.gateway.queue_manager import (
    QueueItem,
    QueueItemNotFoundError,
    QueueManager,
    QueueStatus,
)
from turing.gateway.ring_buffer import RingBuffer, RingBufferConfig
from turing.gateway.service import GatewayService
from turing.gateway.spa import spa_assets_path
from turing.gateway.telemetry_sink import TelemetrySink

__all__ = [
    "COOKIE_NAME",
    "GatewayAuth",
    "GatewayService",
    "QueueItem",
    "QueueItemNotFoundError",
    "QueueManager",
    "QueueStatus",
    "RingBuffer",
    "RingBufferConfig",
    "TelemetrySink",
    "create_app",
    "spa_assets_path",
]
