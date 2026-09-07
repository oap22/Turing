"""scripts/fleet-models.sh — dry-run contract and input validation.

The script only ever shells out over ssh, so the tests drive `--dry-run`
and assert on the exact remote commands it would send; nothing here needs
a fleet.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fleet-models.sh"


def run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "USER": "op", "HOME": "/tmp"},
    )


class TestDryRun:
    def test_pull_builds_idempotent_ollama_commands_per_host(self) -> None:
        res = run("pull", "--dry-run", "--models", "qwen2.5:7b gemma3:1b", "jetson-1", "jetson-2")
        assert res.returncode == 0, res.stderr
        out = res.stdout
        assert out.count("[dry-run] ssh op@jetson-") == 2
        assert "ollama pull" in out
        assert "qwen2.5:7b" in out and "gemma3:1b" in out
        # Present-check before pull, so re-running is a cheap no-op.
        assert "ollama list" in out

    def test_user_option_changes_ssh_target(self) -> None:
        res = run("status", "--dry-run", "--user", "turing", "jetson-1")
        assert res.returncode == 0, res.stderr
        assert "ssh turing@jetson-1" in res.stdout
        assert "ollama ps" in res.stdout

    def test_plan_file_pulls_each_hosts_own_models(self, tmp_path: Path) -> None:
        plan = tmp_path / "models.txt"
        plan.write_text(
            "# fleet layout\n"
            "jetson-1: qwen2.5:7b\n"
            "\n"
            "jetson-2: llama3.2:3b nomic-embed-text  # embeddings node\n"
        )
        res = run("plan", "--dry-run", str(plan))
        assert res.returncode == 0, res.stderr
        out = res.stdout
        j1 = out[out.index("ssh op@jetson-1") : out.index("ssh op@jetson-2")]
        j2 = out[out.index("ssh op@jetson-2") :]
        assert "qwen2.5:7b" in j1 and "llama3.2" not in j1
        assert "llama3.2:3b" in j2 and "nomic-embed-text" in j2 and "qwen2.5" not in j2

    def test_expose_writes_a_systemd_dropin_with_keep_alive(self) -> None:
        res = run(
            "expose",
            "--dry-run",
            "--bind-address",
            "192.168.1.10",
            "--i-understand-unauthenticated",
            "--keep-alive",
            "1h",
            "jetson-3",
        )
        assert res.returncode == 0, res.stderr
        assert "OLLAMA_HOST=192.168.1.10:11434" in res.stdout
        assert "0.0.0.0:11434" not in res.stdout
        assert "OLLAMA_KEEP_ALIVE=1h" in res.stdout
        assert "ollama.service.d" in res.stdout
        assert "systemctl restart ollama" in res.stdout

    def test_prune_keeps_only_listed_models(self) -> None:
        res = run("prune", "--dry-run", "--keep", "qwen2.5:7b", "jetson-1")
        assert res.returncode == 0, res.stderr
        assert "ollama rm" in res.stdout
        assert "qwen2.5:7b" in res.stdout


class TestValidation:
    @pytest.mark.parametrize("bad", ["qwen;whoami", "$(id)", "`x`", "../x", "-flag"])
    def test_rejects_unsafe_model_names(self, bad: str) -> None:
        res = run("pull", "--dry-run", "--models", bad, "jetson-1")
        assert res.returncode == 2
        assert "refusing model name" in res.stderr

    def test_rejects_unsafe_hosts(self) -> None:
        res = run("status", "--dry-run", "jetson-1;whoami")
        assert res.returncode == 2
        assert "refusing host" in res.stderr

    @pytest.mark.parametrize("bad", ["1h' | id; #", "forever", "$(id)", "1 h"])
    def test_rejects_unsafe_keep_alive_values(self, bad: str) -> None:
        res = run(
            "expose",
            "--dry-run",
            "--bind-address",
            "192.168.1.10",
            "--i-understand-unauthenticated",
            "--keep-alive",
            bad,
            "jetson-1",
        )
        assert res.returncode == 2
        assert "refusing keep-alive" in res.stderr

    def test_expose_requires_explicit_bind_and_acknowledgement(self) -> None:
        missing_bind = run("expose", "--dry-run", "--i-understand-unauthenticated", "jetson-1")
        assert missing_bind.returncode == 2
        assert "--bind-address" in missing_bind.stderr

        missing_ack = run("expose", "--dry-run", "--bind-address", "192.168.1.10", "jetson-1")
        assert missing_ack.returncode == 2
        assert "--i-understand-unauthenticated" in missing_ack.stderr

    @pytest.mark.parametrize("bad", ["0.0.0.0;id", "$(id)", "", "[::1]bad"])
    def test_rejects_unsafe_bind_addresses(self, bad: str) -> None:
        res = run(
            "expose",
            "--dry-run",
            "--bind-address",
            bad,
            "--i-understand-unauthenticated",
            "jetson-1",
        )
        assert res.returncode == 2
        assert "bind address" in res.stderr or "--bind-address" in res.stderr

    def test_model_matching_is_literal_not_regex(self) -> None:
        pull = run("pull", "--dry-run", "--models", "qwen2.5:7b", "jetson-1")
        prune = run("prune", "--dry-run", "--keep", "qwen2.5:7b", "jetson-1")
        assert "grep -Fqx 'qwen2.5:7b'" in pull.stdout
        assert 'grep -Fqx "$m"' in prune.stdout

    def test_prune_without_keep_refuses(self) -> None:
        res = run("prune", "--dry-run", "jetson-1")
        assert res.returncode == 2
        assert "--keep" in res.stderr

    def test_no_command_prints_usage(self) -> None:
        res = run("--dry-run")
        assert res.returncode == 2
        assert "Usage" in res.stderr or "usage" in res.stderr.lower()

    def test_no_hosts_refuses(self) -> None:
        res = run("status", "--dry-run")
        assert res.returncode == 2
        assert "no hosts" in res.stderr
