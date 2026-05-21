"""Session-scoped real-broker fixtures for cluster integration tests.

The ``nats_url`` fixture boots a ``nats:2.10`` container via ``testcontainers``
once per pytest session and tears it down on exit. Tests that opt in pay the
~10 s image-pull cost only once.

Skip semantics
--------------
On developer laptops without Docker, the fixture skips with a clear reason
rather than erroring — `pytest tests/` stays green for the no-Docker path.

In CI we treat that skip as a failure: set ``INTEGRATION_REQUIRED=1`` and the
fixture will ``pytest.fail`` instead of skipping when Docker is missing,
turning a silent skip into a loud red CI build.
"""

from __future__ import annotations

import os
import shutil
import socket
import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


def _docker_available() -> bool:
    """Cheap pre-flight: does ``docker`` exist on PATH and respond?"""
    if shutil.which("docker") is None:
        return False
    import subprocess

    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _skip_or_fail(reason: str) -> None:
    if os.environ.get("INTEGRATION_REQUIRED") == "1":
        pytest.fail(f"integration required but {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="session")
def nats_url() -> Iterator[str]:
    """Boot ``nats:2.10`` once per session and yield a ``nats://host:port`` URL.

    Image tag is pinned to ``nats:2.10``. To bump it, edit the constant in
    this file and update ``tests/integration/README.md``.
    """
    if not _docker_available():
        _skip_or_fail("docker unavailable")

    try:
        from testcontainers.core.container import DockerContainer
        from testcontainers.core.waiting_utils import wait_for_logs
    except ImportError:
        _skip_or_fail("testcontainers not installed")

    container = DockerContainer("nats:2.10").with_exposed_ports(4222)
    try:
        container.start()
    except Exception as exc:  # pragma: no cover — docker hiccup path
        _skip_or_fail(f"docker container start failed: {exc}")

    try:
        # NATS prints this once the listener is accepting connections.
        wait_for_logs(container, "Server is ready", timeout=30)
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(4222))

        # Belt-and-braces: confirm we can actually open a TCP connection
        # before yielding. Caught a flaky CI start once.
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with socket.create_connection((host, port), timeout=1):
                    break
            except OSError:
                time.sleep(0.2)
        else:  # pragma: no cover — only fires on a broken container start
            pytest.fail(f"nats container never accepted TCP on {host}:{port}")

        yield f"nats://{host}:{port}"
    finally:
        container.stop()
