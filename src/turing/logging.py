"""Structured logging setup using structlog."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from turing.config import TuringConfig


def setup_logging(config: TuringConfig) -> None:
    """Configure structlog and stdlib logging for the application.

    In **production** mode, log entries are emitted as JSON lines suitable for
    aggregation by tools such as Loki or CloudWatch.  In **development** mode,
    coloured, human-readable output is used instead.

    The ``node_name`` from *config* is automatically bound to every log entry
    so that multi-node deployments can be distinguished.

    Parameters
    ----------
    config:
        The application configuration object.
    """

    log_level = getattr(logging, config.log_level.upper(), logging.INFO)

    # Shared pre-chain processors applied before the final renderer.
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if config.is_production:
        # ── Production: JSON output ──────────────────────────────────
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        # ── Development: pretty console output ───────────────────────
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    # Apply to the root logger so both structlog and stdlib logging are
    # formatted consistently.
    root_logger = logging.getLogger()
    root_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)

    # Silence noisy third-party loggers.
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(max(log_level, logging.WARNING))

    # Bind the node name globally so every subsequent log line includes it.
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(node_name=config.node_name)

    logger = structlog.get_logger("turing.logging")
    logger.info(
        "logging_configured",
        log_level=config.log_level,
        renderer="json" if config.is_production else "console",
        node_name=config.node_name,
    )
