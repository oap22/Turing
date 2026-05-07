"""Tests for the gateway-related fields on TuringConfig."""

from __future__ import annotations

from unittest.mock import patch

from turing.config import TuringConfig


class TestGatewayDefaults:
    def test_disabled_by_default(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.gateway_enabled is False

    def test_default_port(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.gateway_port == 8765

    def test_default_bind_is_tailscale_interface(self) -> None:
        """Default bind must NOT be 0.0.0.0 — Tailscale only.

        We don't hardcode a Tailscale IP here because it varies per host;
        '127.0.0.1' is the conservative default that effectively requires
        explicit operator action to expose externally.
        """
        with patch.dict("os.environ", {}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.gateway_bind != "0.0.0.0"

    def test_token_default_is_empty(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.gateway_token == ""


class TestGatewayOverrides:
    def test_enabled_via_env(self) -> None:
        with patch.dict(
            "os.environ", {"TURING_GATEWAY_ENABLED": "true"}, clear=True
        ):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.gateway_enabled is True

    def test_token_via_env(self) -> None:
        with patch.dict(
            "os.environ", {"TURING_GATEWAY_TOKEN": "abc-123"}, clear=True
        ):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.gateway_token == "abc-123"

    def test_port_via_env(self) -> None:
        with patch.dict(
            "os.environ", {"TURING_GATEWAY_PORT": "9000"}, clear=True
        ):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.gateway_port == 9000

    def test_bind_via_env(self) -> None:
        with patch.dict(
            "os.environ", {"TURING_GATEWAY_BIND": "100.64.0.1"}, clear=True
        ):
            cfg = TuringConfig(_env_file=None)  # type: ignore[call-arg]
        assert cfg.gateway_bind == "100.64.0.1"
