"""Tests for turing.config.TuringConfig."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from turing.config import TuringConfig


class TestDefaults:
    """Verify sensible defaults when no env vars are set."""

    def test_default_node_name(self, mock_config: TuringConfig) -> None:
        assert mock_config.node_name == "test-node"

    def test_default_command_prefix(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.discord_command_prefix == "!turing"

    def test_default_ollama_host(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.ollama_host == "http://localhost:11434"

    def test_default_ollama_model(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.ollama_model == "gemma3:1b"

    def test_default_anthropic_model(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.anthropic_model == "claude-sonnet-4-20250514"

    def test_default_routing_mode(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.llm_routing_mode == "auto"

    def test_default_mesh_disabled(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.mesh_enabled is False

    def test_default_sandbox_enabled(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.sandbox_enabled is True

    def test_default_env_is_development(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.env == "development"

    def test_default_log_level(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.log_level == "INFO"

    def test_default_learning_auto_extract(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.learning_auto_extract is True

    def test_default_learning_extract_interval(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.learning_extract_interval == 5

    def test_default_telemetry_prompt_sample_max_bytes(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.telemetry_prompt_sample_max_bytes == 2048

    def test_telemetry_prompt_sample_max_bytes_override(self) -> None:
        with patch.dict(
            "os.environ",
            {"TURING_TELEMETRY_PROMPT_SAMPLE_MAX_BYTES": "512"},
            clear=True,
        ):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.telemetry_prompt_sample_max_bytes == 512


class TestEnvOverrides:
    """Verify that environment variables correctly override defaults."""

    def test_node_name_override(self) -> None:
        with patch.dict("os.environ", {"TURING_NODE_NAME": "custom-node"}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.node_name == "custom-node"

    def test_discord_token_override(self) -> None:
        with patch.dict("os.environ", {"TURING_DISCORD_TOKEN": "tok-abc"}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.discord_token == "tok-abc"

    def test_anthropic_key_override(self) -> None:
        with patch.dict("os.environ", {"TURING_ANTHROPIC_API_KEY": "sk-test"}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.anthropic_api_key == "sk-test"

    def test_routing_mode_override(self) -> None:
        with patch.dict("os.environ", {"TURING_LLM_ROUTING_MODE": "cloud"}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.llm_routing_mode == "cloud"

    def test_mesh_port_override(self) -> None:
        with patch.dict("os.environ", {"TURING_MESH_PORT": "9999"}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.mesh_port == 9999

    def test_sandbox_timeout_override(self) -> None:
        with patch.dict("os.environ", {"TURING_SANDBOX_TIMEOUT": "60"}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.sandbox_timeout == 60

    def test_log_level_override(self) -> None:
        with patch.dict("os.environ", {"TURING_LOG_LEVEL": "DEBUG"}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.log_level == "DEBUG"

    def test_env_production_override(self) -> None:
        with patch.dict("os.environ", {"TURING_ENV": "production"}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.env == "production"
        assert cfg.is_production is True

    def test_admin_ids_override(self) -> None:
        with patch.dict(
            "os.environ",
            {"TURING_DISCORD_ADMIN_IDS": "[123, 456]"},
            clear=True,
        ):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.discord_admin_ids == [123, 456]

    def test_allowed_write_paths_override(self) -> None:
        with patch.dict(
            "os.environ",
            {"TURING_ALLOWED_WRITE_PATHS": '["/var/data", "/opt/out"]'},
            clear=True,
        ):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.allowed_write_paths == ["/var/data", "/opt/out"]


class TestNodeIdAutoGeneration:
    """The node_id field must be auto-generated when left empty."""

    def test_node_id_generated_when_empty(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        # Must be a valid UUID
        parsed = uuid.UUID(cfg.node_id)
        assert parsed.version == 4

    def test_node_id_not_overridden_when_provided(self) -> None:
        explicit_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with patch.dict("os.environ", {"TURING_NODE_ID": explicit_id}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.node_id == explicit_id

    def test_two_configs_get_different_ids(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            cfg1 = TuringConfig(_env_file=None)  # type: ignore[call-arg]
            cfg2 = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg1.node_id != cfg2.node_id


class TestPathValidation:
    """Validators should resolve paths and create parent directories."""

    def test_db_parent_directory_created(self, tmp_path: Path) -> None:
        db_path = tmp_path / "nested" / "deep" / "turing.db"
        with patch.dict("os.environ", {"TURING_DB_PATH": str(db_path)}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.db_path.parent.exists()
        assert cfg.db_path.is_absolute()

    def test_db_path_is_resolved(self, tmp_path: Path) -> None:
        # Use a relative-looking path
        db_path = tmp_path / "data" / "turing.db"
        with patch.dict("os.environ", {"TURING_DB_PATH": str(db_path)}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.db_path.is_absolute()

    def test_embedding_path_is_resolved(self, tmp_path: Path) -> None:
        emb_path = tmp_path / "models" / "miniLM"
        with patch.dict(
            "os.environ",
            {"TURING_EMBEDDING_MODEL_PATH": str(emb_path)},
            clear=True,
        ):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.embedding_model_path.is_absolute()


class TestIsProduction:
    """The ``is_production`` property reflects the env field."""

    def test_development_is_not_production(self) -> None:
        with patch.dict("os.environ", {"TURING_ENV": "development"}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.is_production is False

    def test_staging_is_not_production(self) -> None:
        with patch.dict("os.environ", {"TURING_ENV": "staging"}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.is_production is False

    def test_production_is_production(self) -> None:
        with patch.dict("os.environ", {"TURING_ENV": "production"}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.is_production is True
