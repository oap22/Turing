"""Application configuration via Pydantic Settings with .env file support."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from turing.llm.endpoints import validate_ollama_allowlist, validate_ollama_endpoint
from turing.worker.tools.web_fetch import DEFAULT_ALLOWED_HOSTS


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
    ollama_tools_enabled: bool = Field(
        default=False,
        description=(
            "Opt in to passing tool definitions to the local Ollama model; "
            "disabled by default because model tool compliance varies"
        ),
    )

    # ── Native agent mailbox (optional) ─────────────────────────────────
    # The mailbox is a dedicated SQLite store shared by cooperating local
    # processes.  All three settings are required together; leaving all unset
    # keeps the native tool disabled.
    agent_mailbox_db: Path | None = Field(
        default=None,
        description="Dedicated SQLite path for the native agent mailbox",
    )
    agent_mailbox_workflow: str | None = Field(
        default=None,
        description="Workflow namespace for the native agent mailbox",
    )
    agent_mailbox_agent: str | None = Field(
        default=None,
        description="Fixed agent identity used by the native mailbox tool",
    )

    ollama_keep_alive: str | None = Field(
        default="30m",
        description="How long Ollama keeps the model resident after a request "
        "(Ollama keep_alive syntax: '30m', '1h', '-1' = forever, '0' = unload "
        "at once; unset = Ollama's own 5m default). On Pi/Jetson-class hardware "
        "reloading a model between conversational turns is the dominant "
        "local-path latency, so the default pins it for half an hour.",
    )
    ollama_num_ctx: int | None = Field(
        default=None,
        description="Context window in tokens passed to Ollama as num_ctx; unset "
        "keeps the model's own default",
    )
    ollama_warmup: bool = Field(
        default=True,
        description="Load the local model into memory at startup (in the "
        "background) so the first local turn is not a cold start",
    )
    ollama_advertise_host: str | None = Field(
        default=None,
        description="URL peers should use to reach THIS node's Ollama, e.g. "
        "http://jetson-1:11434. Advertised in mesh presence heartbeats so "
        "other nodes can route local-tier requests here for models they have "
        "not pulled. Unset = never advertised, peers never target this node. "
        "Ollama must listen on a LAN-reachable interface for this to work "
        "(configure an explicit bind address with the acknowledged "
        "scripts/fleet-models.sh expose command).",
    )
    ollama_peer_allowlist: list[str] = Field(
        default_factory=list,
        description="Exact Ollama endpoint URLs authorized for peer routing. "
        "Presence advertisements are never authority by themselves; each URL "
        "must be listed here by the operator.",
    )
    llm_peer_models_enabled: bool = Field(
        default=True,
        description="When mesh is enabled, let the local tier borrow a peer's "
        "Ollama for the configured model if this node has not pulled it "
        "(turing.llm.pool). Off = the local tier is always this node.",
    )
    llm_peer_fallback_enabled: bool = Field(
        default=False,
        description="Explicitly allow cloud-auth fallback to use a peer Ollama. "
        "Normal local-to-peer routing remains controlled separately by "
        "llm_peer_models_enabled.",
    )

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
    # Presence is signed (issue #348): every heartbeat/leave rides the same
    # SignedTransport envelope the dispatch pipeline uses. Both fields below
    # are REQUIRED for presence to start when mesh_enabled — fail-closed:
    # unset means presence stays off and the node runs as a singleton.
    mesh_signing_seed: str | None = Field(
        default=None,
        description="Hex-encoded 32-byte Ed25519 private-key seed this node "
        "signs mesh presence messages with. Required (together with "
        "mesh_trusted_keys) for presence to start; unset disables presence "
        "(fail-closed). Env: TURING_MESH_SIGNING_SEED.",
    )
    mesh_trusted_keys: dict[str, str] | None = Field(
        default=None,
        description="JSON object mapping node_id -> hex-encoded Ed25519 "
        "public key for every trusted mesh node (including this one). A "
        "presence message is only accepted when its signature verifies "
        "against the key bound to the claimed sender_id. Required for "
        "presence to start; unset disables presence (fail-closed). "
        "Env: TURING_MESH_TRUSTED_KEYS.",
    )

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
    safety_auto_approve_high_risk: bool = Field(
        default=False,
        description="Permit HIGH-risk auto-approval only for an authenticated "
        "admin or safety_single_operator_user_id. The shell deny-list still "
        "applies and everything is still audit-logged. Env: "
        "TURING_SAFETY_AUTO_APPROVE_HIGH_RISK.",
    )
    safety_single_operator_user_id: str | None = Field(
        default=None,
        description="Explicit authenticated operator identity permitted to use "
        "safety_auto_approve_high_risk when not listed as an admin",
    )
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
    web_fetch_allowed_hosts: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ALLOWED_HOSTS),
        description="Host allowlist for the worker-local web_fetch grounding tool "
        "(ADR 0009 §2). Each entry admits the host and its subdomains; the default "
        "covers research-paper sources (arXiv, Google Scholar via scholar.google.com, "
        "Semantic Scholar, OpenReview, …). This is the contract the worker grounding "
        "assembly consumes when it lands — nothing reads it in Phase 0 yet. JSON "
        "list in env: TURING_WEB_FETCH_ALLOWED_HOSTS.",
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

    @model_validator(mode="after")
    def _validate_agent_mailbox_binding(self) -> TuringConfig:
        """Require the native mailbox database, workflow, and agent together."""
        values = (self.agent_mailbox_db, self.agent_mailbox_workflow, self.agent_mailbox_agent)
        if any(value not in (None, "") for value in values) and not all(
            value not in (None, "") for value in values
        ):
            raise ValueError(
                "TURING_AGENT_MAILBOX_DB, TURING_AGENT_MAILBOX_WORKFLOW, and "
                "TURING_AGENT_MAILBOX_AGENT must be configured together"
            )
        if self.agent_mailbox_db is not None:
            self.agent_mailbox_db = self.agent_mailbox_db.expanduser().resolve()
            self.agent_mailbox_db.parent.mkdir(parents=True, exist_ok=True)

    @model_validator(mode="after")
    def _validate_ollama_peer_authority(self) -> TuringConfig:
        """Require self-advertisement to be explicitly operator-authorized."""
        if self.ollama_advertise_host is not None and (
            self.ollama_advertise_host not in self.ollama_peer_allowlist
        ):
            raise ValueError(
                "TURING_OLLAMA_ADVERTISE_HOST must be included exactly in "
                "TURING_OLLAMA_PEER_ALLOWLIST"
            )
        return self

    @field_validator("db_path", mode="after")
    @classmethod
    def _ensure_db_parent_dir(cls, v: Path) -> Path:
        """Create parent directories for the database file if they don't exist."""
        resolved = v.resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    @field_validator("agent_mailbox_db", mode="before")
    @classmethod
    def _empty_agent_mailbox_db_is_unset(cls, v: object) -> object:
        """Reject the SQLite memory sentinel before resolving a durable path."""
        if isinstance(v, (str, Path)) and str(v) == ":memory:":
            raise ValueError("agent mailbox requires a durable file, not :memory:")
        return None if v == "" else v

    @field_validator("ollama_host", "ollama_advertise_host", mode="after")
    @classmethod
    def _validate_ollama_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return validate_ollama_endpoint(v)

    @field_validator("ollama_peer_allowlist", mode="after")
    @classmethod
    def _validate_ollama_peer_urls(cls, v: list[str]) -> list[str]:
        return validate_ollama_allowlist(v)

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
