"""Application configuration via Pydantic Settings with .env file support."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TuringConfig(BaseSettings):
    """Central configuration for the Turing assistant.

    All fields can be set via environment variables prefixed with ``TURING_``.
    A ``.env`` file in the working directory is loaded automatically.
    """

    model_config = SettingsConfigDict(
        env_prefix="TURING_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Node identity ────────────────────────────────────────────────────
    node_name: str = Field(default="pi-alpha", description="Human-readable node name")
    node_id: str = Field(default="", description="Unique node UUID (auto-generated if empty)")

    # ── Operator / admin ─────────────────────────────────────────────────
    # Surface-agnostic admin allow-list (ADR 0010 retired the Discord task bot
    # and its Discord-specific admin IDs). The safety gate auto-approves
    # HIGH-risk tool calls for these users. Entries are opaque identifiers;
    # whichever operator surface is in front sets ``user_id`` to match.
    admin_user_ids: list[str] = Field(
        default_factory=list,
        description="List of operator user IDs with admin privileges",
    )

    # ── Alerts (closed-laptop fallback) ──────────────────────────────────
    # Per ADR-0010 §2, ntfy (self-hosted on the Surface coordinator) replaces
    # the Discord-DM closed-laptop fallback. Topic is per operator. Lives on
    # the coordinator only; the alerts dispatcher posts here when the SPA
    # telemetry sink has gone stale (slice B wires the transport).
    operator_ntfy_topic: str | None = Field(
        default=None,
        validation_alias="TURING_OPERATOR_NTFY_TOPIC",
        description="ntfy topic that receives hardware-safety alert pushes "
        "when the SPA is unreachable; coordinator-only, unset disables the "
        "ntfy fallback",
    )
    coordinator_ntfy_base_url: str | None = Field(
        default=None,
        validation_alias="TURING_COORDINATOR_NTFY_BASE_URL",
        description="ntfy server base URL on the Surface coordinator "
        "(e.g. http://surface.<tailnet>.ts.net:8090); coordinator-only, "
        "unset disables the ntfy fallback",
    )

    # ── Anthropic (cloud LLM) ────────────────────────────────────────────
    anthropic_api_key: str = Field(default="", description="Anthropic API key")
    anthropic_model: str = Field(
        default="claude-sonnet-4-20250514",
        description="Anthropic model identifier",
    )

    # ── Ollama (local LLM) ───────────────────────────────────────────────
    ollama_host: str = Field(
        default="http://localhost:11434",
        description="Ollama server base URL",
    )
    ollama_model: str = Field(default="gemma3:1b", description="Default Ollama model")

    # ── LLM routing ─────────────────────────────────────────────────────
    llm_routing_mode: Literal["local", "cloud", "auto"] = Field(
        default="auto",
        description="LLM routing strategy: local-only, cloud-only, or auto",
    )

    # ── Storage ──────────────────────────────────────────────────────────
    db_path: Path = Field(
        default=Path("./data/turing.db"),
        description="Path to the SQLite database file",
    )
    embedding_model_path: Path = Field(
        default=Path("./models/all-MiniLM-L6-v2"),
        description="Path to the ONNX embedding model directory",
    )
    vault_root: Path = Field(
        default=Path("/home/turing/vault"),
        description=(
            "Working-tree root of the vault git repository. The vault watcher "
            "diffs each new commit against its parent and reindexes only the "
            "changed markdown files (ADR 0010 §4)."
        ),
    )
    vault_poll_interval_seconds: float = Field(
        default=5.0,
        description="Seconds the vault watcher sleeps between git HEAD polls",
    )

    # ── Mesh networking ──────────────────────────────────────────────────
    # Per ADR-0008, peer presence rides on the shared NATS bus
    # (subjects ``mesh.presence.*``); there is no separate mesh port.
    mesh_enabled: bool = Field(default=False, description="Enable mesh peer presence")

    # ── Runtime bus (NATS) ───────────────────────────────────────────────
    nats_url: str = Field(
        default="nats://127.0.0.1:4222",
        description="NATS server URL the coordinator publishes and workers dial",
    )
    nats_lan_only: bool = Field(
        default=True,
        description="Refuse non-LAN NATS endpoints unless explicitly flipped",
    )
    nats_tls_enabled: bool = Field(
        default=True,
        description="Require TLS for NATS connections",
    )
    nats_nkey_seed: str | None = Field(
        default=None,
        description="NATS nkey seed for authentication; optional in local dev",
    )

    # ── Sandbox / security ───────────────────────────────────────────────
    sandbox_enabled: bool = Field(default=True, description="Enable bubblewrap sandbox for tools")
    sandbox_timeout: int = Field(default=30, description="Sandbox execution timeout in seconds")
    allowed_write_paths: list[str] = Field(
        default_factory=lambda: ["/tmp", "/home/turing/data"],
        description="Filesystem paths the sandbox may write to",
    )
    allowed_plugins: list[str] | None = Field(
        default=None,
        description="Allow-list of plugin names permitted to load from plugins/. "
        "Loading a plugin runs arbitrary code, so this is fail-closed: unset "
        '(the default) loads NO plugins. Use ["*"] to load every plugin found, '
        "or a JSON list of names. Env: TURING_ALLOWED_PLUGINS.",
    )

    # ── Telemetry ────────────────────────────────────────────────────────
    telemetry_prompt_sample_max_bytes: int = Field(
        default=2048,
        description="Per-event byte budget for redacted prompt/response samples",
    )

    # ── Gateway (pi-alpha web UI) ────────────────────────────────────────
    gateway_enabled: bool = Field(
        default=False,
        description="Run the in-process FastAPI gateway (pi-alpha only)",
    )
    gateway_token: str = Field(
        default="",
        description="Bearer token required for gateway HTTP/WS access",
    )
    gateway_bind: str = Field(
        default="127.0.0.1",
        description="Interface the gateway binds to; default is loopback "
        "so binding externally requires explicit operator action",
    )
    gateway_port: int = Field(
        default=8765,
        description="TCP port the gateway listens on",
    )

    # ── Budget ───────────────────────────────────────────────────────────
    budget_daily_cap_cents: int = Field(
        default=1000,
        description="Hard cap on cloud-LLM spend per local day, in cents (default $10)",
    )

    # ── Learning ─────────────────────────────────────────────────────────
    learning_auto_extract: bool = Field(
        default=True,
        description="Automatically extract knowledge from conversations",
    )
    learning_extract_interval: int = Field(
        default=5,
        description="Run extraction every N conversation turns",
    )

    # ── Environment ──────────────────────────────────────────────────────
    env: Literal["development", "staging", "production"] = Field(
        default="development",
        description="Runtime environment",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Minimum log level",
    )

    # ── Validators ───────────────────────────────────────────────────────

    @model_validator(mode="after")
    def _autogenerate_node_id(self) -> TuringConfig:
        """Generate a stable UUID for the node when none is provided."""
        if not self.node_id:
            self.node_id = str(uuid.uuid4())
        return self

    @field_validator("db_path", mode="after")
    @classmethod
    def _ensure_db_parent_dir(cls, v: Path) -> Path:
        """Create parent directories for the database file if they don't exist."""
        resolved = v.resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    @field_validator("embedding_model_path", mode="after")
    @classmethod
    def _resolve_embedding_path(cls, v: Path) -> Path:
        """Resolve the embedding model path to an absolute path."""
        return v.resolve()

    # ── Derived helpers ──────────────────────────────────────────────────

    @property
    def is_production(self) -> bool:
        """Return True when running in the production environment."""
        return self.env == "production"
