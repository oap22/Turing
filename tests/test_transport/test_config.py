"""Tests for transport-related config flags on TuringConfig.

ADR 0001 / Issue #4: NATS bound LAN-only with TLS + nkey auth wired, public
flip is a config change rather than a refactor.
"""

from __future__ import annotations

from turing.config import TuringConfig


def test_nats_defaults_are_lan_only_and_secure() -> None:
    config = TuringConfig(_env_file=None)

    assert config.nats_lan_only is True
    assert config.nats_tls_enabled is True
    assert config.nats_url == "nats://127.0.0.1:4222"


def test_public_flip_disables_lan_only_without_code_change() -> None:
    config = TuringConfig(
        _env_file=None,
        nats_lan_only=False,
        nats_url="tls://nats.example.com:4222",
        nats_nkey_seed="SUACSSL3UAHUDXKFSNVUZRF5UHPMWZ6BFDTJ7M6USDXIEDNPPQYYYCU3VY",
    )

    assert config.nats_lan_only is False
    assert config.nats_url.startswith("tls://")
    assert config.nats_nkey_seed is not None


def test_nkey_seed_is_optional_for_local_dev() -> None:
    config = TuringConfig(_env_file=None)
    assert config.nats_nkey_seed is None
