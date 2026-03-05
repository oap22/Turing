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

    # ── Discord ──────────────────────────────────────────────────────────
    discord_token: str = Field(default="", description="Discord bot token")
    discord_admin_ids: list[int] = Field(
        default_factory=list,
        description="List of Discord user IDs with admin privileges",
    )
    discord_command_prefix: str = Field(
        default="!turing",
        description="Prefix for bot commands",
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

    # ── Mesh networking ──────────────────────────────────────────────────
    mesh_enabled: bool = Field(default=False, description="Enable ZeroMQ mesh networking")
    mesh_port: int = Field(default=5670, description="ZeroMQ mesh port")

    # ── Sandbox / security ───────────────────────────────────────────────
    sandbox_enabled: bool = Field(default=True, description="Enable bubblewrap sandbox for tools")
    sandbox_timeout: int = Field(default=30, description="Sandbox execution timeout in seconds")
    allowed_write_paths: list[str] = Field(
        default_factory=lambda: ["/tmp", "/home/turing/data"],
        description="Filesystem paths the sandbox may write to",
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
    def _autogenerate_node_id(self) -> "TuringConfig":
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
