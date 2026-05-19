"""Collect per-node specs for the fleet specs panel.

Slice 1/3 (#215) introduced the tracer fields (cpu_percent, temp_celsius);
slice 2/3 (#216) grows the schema to the full PRD shape — static hardware
identity (model_name, os, arch, cpu_cores, ram_total_bytes, disk_total_bytes)
plus the remaining live signals (mem_used_bytes, disk_used_bytes,
uptime_seconds, loadavg). Static fields are cached on first call.

Per-OS branches extend the slice-1 ones: ``/proc/device-tree/model`` on
Linux for Pi/Jetson hardware identity, ``sysctl hw.model`` on Darwin.
"""

from __future__ import annotations

import glob
import os
import platform
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Any

import psutil


@dataclass
class NodeSpecs:
    """Full per-node specs payload (#216 schema).

    Static fields are sampled once at startup and cached for subsequent
    heartbeats; live fields are recollected each heartbeat. ``None`` /
    ``"unknown"`` values are tolerated so unsupported platforms don't
    drop the heartbeat — the UI shows an em-dash instead.
    """

    # --- Static (cached after first call) ---
    model_name: str
    os: str
    arch: str
    cpu_cores: int
    ram_total_bytes: int
    disk_total_bytes: int

    # --- Live (recollected per heartbeat) ---
    cpu_percent: float
    mem_used_bytes: int
    disk_used_bytes: int
    temp_celsius: float | None
    uptime_seconds: int
    loadavg_1m: float
    loadavg_5m: float
    loadavg_15m: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NodeSpecs:
        raw_temp = data.get("temp_celsius")
        temp = None if raw_temp is None else float(raw_temp)
        return cls(
            model_name=str(data.get("model_name", "unknown")),
            os=str(data.get("os", "unknown")),
            arch=str(data.get("arch", "unknown")),
            cpu_cores=int(data.get("cpu_cores", 0)),
            ram_total_bytes=int(data.get("ram_total_bytes", 0)),
            disk_total_bytes=int(data.get("disk_total_bytes", 0)),
            cpu_percent=float(data.get("cpu_percent", 0.0)),
            mem_used_bytes=int(data.get("mem_used_bytes", 0)),
            disk_used_bytes=int(data.get("disk_used_bytes", 0)),
            temp_celsius=temp,
            uptime_seconds=int(data.get("uptime_seconds", 0)),
            loadavg_1m=float(data.get("loadavg_1m", 0.0)),
            loadavg_5m=float(data.get("loadavg_5m", 0.0)),
            loadavg_15m=float(data.get("loadavg_15m", 0.0)),
        )


# Pi 4 / Pi 5 expose CPU temperature here. Jetson exposes per-zone files
# under ``thermal_zone*/temp`` (CPU, GPU, AO, etc.) — we take the max.
_PI_THERMAL_ZONE = "/sys/class/thermal/thermal_zone0/temp"
_THERMAL_ZONES_GLOB = "/sys/class/thermal/thermal_zone*/temp"
_DEVICE_TREE_MODEL = "/proc/device-tree/model"

# Disk usage targets the root filesystem — the only path guaranteed to
# exist on every supported node.
_DISK_PATH = "/"


def _read_pi_temp() -> float | None:
    try:
        with open(_PI_THERMAL_ZONE) as fh:
            millideg = int(fh.read().strip())
        return millideg / 1000.0
    except (OSError, ValueError):
        return None


def _read_jetson_max_temp() -> float | None:
    zones = glob.glob(_THERMAL_ZONES_GLOB)
    if not zones:
        return None
    values: list[float] = []
    for path in zones:
        try:
            with open(path) as fh:
                millideg = int(fh.read().strip())
        except (OSError, ValueError):
            continue
        # Jetson zones occasionally report wildly negative sentinel values
        # for inactive sensors. Filter them so ``max`` reflects reality.
        celsius = millideg / 1000.0
        if celsius > -50.0:
            values.append(celsius)
    if not values:
        return None
    return max(values)


def _is_jetson() -> bool:
    """Best-effort Jetson detection.

    Jetson L4T images expose ``/etc/nv_tegra_release``; that file is the
    canonical marker referenced by NVIDIA's own tooling.
    """
    try:
        with open("/etc/nv_tegra_release"):
            return True
    except OSError:
        return False


def _collect_temperature(system: str) -> float | None:
    if system == "Darwin":
        # No clean path on Mac without sudo or a third-party SMC tool; the
        # specs panel shows an em-dash in this column for Mac rows.
        return None
    if system != "Linux":
        return None
    if _is_jetson():
        return _read_jetson_max_temp()
    return _read_pi_temp()


def _read_linux_model_name() -> str:
    """Read the Pi / Jetson hardware identity from device-tree.

    The file is NUL-terminated; strip both nulls and whitespace.
    """
    try:
        with open(_DEVICE_TREE_MODEL) as fh:
            raw = fh.read()
    except OSError:
        return "unknown"
    return raw.replace("\x00", "").strip() or "unknown"


def _read_darwin_model_name() -> str:
    """Read the Mac hardware identity via ``sysctl hw.model``."""
    try:
        result = subprocess.run(
            ["sysctl", "-n", "hw.model"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip() or "unknown"


def _collect_model_name(system: str) -> str:
    if system == "Linux":
        return _read_linux_model_name()
    if system == "Darwin":
        return _read_darwin_model_name()
    return "unknown"


def _collect_loadavg() -> tuple[float, float, float]:
    try:
        one, five, fifteen = os.getloadavg()
    except (OSError, AttributeError):
        return (0.0, 0.0, 0.0)
    return (float(one), float(five), float(fifteen))


# Module-level cache for the static portion of NodeSpecs. Sampled once on
# the first ``collect_specs`` call and reused thereafter — these values
# don't change between heartbeats.
_STATIC_CACHE: dict[str, Any] | None = None


def _collect_static() -> dict[str, Any]:
    global _STATIC_CACHE
    if _STATIC_CACHE is not None:
        return _STATIC_CACHE
    system = platform.system()
    machine = platform.machine() or "unknown"
    cpu_cores = psutil.cpu_count(logical=True) or 0
    ram_total = int(psutil.virtual_memory().total)
    try:
        disk_total = int(psutil.disk_usage(_DISK_PATH).total)
    except OSError:
        disk_total = 0
    _STATIC_CACHE = {
        "model_name": _collect_model_name(system),
        "os": system.lower() if system else "unknown",
        "arch": machine,
        "cpu_cores": int(cpu_cores),
        "ram_total_bytes": ram_total,
        "disk_total_bytes": disk_total,
    }
    return _STATIC_CACHE


def _reset_static_cache_for_tests() -> None:
    """Test helper — drop the cached static block so the next call resamples."""
    global _STATIC_CACHE
    _STATIC_CACHE = None


def collect_specs() -> NodeSpecs:
    """Sample the current node's full specs.

    Static fields are cached on first call. Safe to call from a presence
    heartbeat — bounded, no sudo (the Darwin model-name path runs
    ``sysctl`` once at startup), no long-running shell.
    """
    static = _collect_static()
    cpu_percent = float(psutil.cpu_percent(interval=None))
    vm = psutil.virtual_memory()
    mem_used = int(getattr(vm, "used", 0))
    try:
        disk = psutil.disk_usage(_DISK_PATH)
        disk_used = int(disk.used)
    except OSError:
        disk_used = 0
    temp_celsius = _collect_temperature(platform.system())
    try:
        boot = float(psutil.boot_time())
        uptime = max(0, int(time.time() - boot))
    except Exception:
        uptime = 0
    one, five, fifteen = _collect_loadavg()
    return NodeSpecs(
        model_name=str(static["model_name"]),
        os=str(static["os"]),
        arch=str(static["arch"]),
        cpu_cores=int(static["cpu_cores"]),
        ram_total_bytes=int(static["ram_total_bytes"]),
        disk_total_bytes=int(static["disk_total_bytes"]),
        cpu_percent=cpu_percent,
        mem_used_bytes=mem_used,
        disk_used_bytes=disk_used,
        temp_celsius=temp_celsius,
        uptime_seconds=uptime,
        loadavg_1m=one,
        loadavg_5m=five,
        loadavg_15m=fifteen,
    )
