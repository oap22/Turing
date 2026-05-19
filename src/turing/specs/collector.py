"""Collect per-node specs for the fleet specs panel.

Slice 1/3 (#215) defines the tracer subset: CPU% and temperature. Per-OS
branching for temperature is the trickiest bit of the whole feature, so we
shake it out first — multi-zone Jetson, single-zone Pi, no-clean-path Mac.
Later slices grow the schema in place without breaking the wire format.
"""

from __future__ import annotations

import glob
import platform
from dataclasses import asdict, dataclass
from typing import Any

import psutil


@dataclass
class NodeSpecs:
    """Tracer subset of per-node specs.

    Future slices extend this with memory, disk, model identity, hardware
    identity, etc. Missing/unknown fields are represented as ``None`` so
    the schema can grow without breaking rolling upgrades.
    """

    cpu_percent: float
    temp_celsius: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NodeSpecs:
        cpu = float(data.get("cpu_percent", 0.0))
        raw_temp = data.get("temp_celsius")
        temp = None if raw_temp is None else float(raw_temp)
        return cls(cpu_percent=cpu, temp_celsius=temp)


# Pi 4 / Pi 5 expose CPU temperature here. Jetson exposes per-zone files
# under ``thermal_zone*/temp`` (CPU, GPU, AO, etc.) — we take the max.
_PI_THERMAL_ZONE = "/sys/class/thermal/thermal_zone0/temp"
_THERMAL_ZONES_GLOB = "/sys/class/thermal/thermal_zone*/temp"


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


def collect_specs() -> NodeSpecs:
    """Sample the current node's tracer specs.

    Safe to call from a presence heartbeat — bounded, no sudo, no shell.
    The ``cpu_percent`` reading uses ``interval=None`` so it returns the
    CPU usage since the *previous* call rather than blocking for a sample
    window; the heartbeat loop calls this often enough that the value is
    meaningful by the second tick.
    """
    cpu_percent = float(psutil.cpu_percent(interval=None))
    temp_celsius = _collect_temperature(platform.system())
    return NodeSpecs(cpu_percent=cpu_percent, temp_celsius=temp_celsius)
