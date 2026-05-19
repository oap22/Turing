"""Tests for :mod:`turing.specs.collector`.

Per-OS branching is the riskiest part of the specs panel feature; this
module covers each branch (Mac, Pi single-zone, Jetson multi-zone) with
``platform.system()`` and filesystem access mocked.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import mock_open, patch

import pytest

from turing.specs import collector as collector_mod
from turing.specs.collector import NodeSpecs, collect_specs


@pytest.fixture(autouse=True)
def _reset_static_cache() -> None:
    collector_mod._reset_static_cache_for_tests()


def _vm(used: int = 0, total: int = 8 * 1024**3) -> SimpleNamespace:
    return SimpleNamespace(total=total, used=used, available=total - used)


def _du(used: int = 0, total: int = 128 * 1024**3) -> SimpleNamespace:
    return SimpleNamespace(total=total, used=used, free=total - used)


class TestNodeSpecsRoundTrip:
    def test_to_dict_round_trip(self) -> None:
        original = NodeSpecs(
            model_name="Raspberry Pi 5",
            os="linux",
            arch="aarch64",
            cpu_cores=4,
            ram_total_bytes=8 * 1024**3,
            disk_total_bytes=128 * 1024**3,
            cpu_percent=12.5,
            mem_used_bytes=2 * 1024**3,
            disk_used_bytes=10 * 1024**3,
            temp_celsius=48.0,
            uptime_seconds=3600,
            loadavg_1m=0.5,
            loadavg_5m=0.4,
            loadavg_15m=0.3,
        )
        d = original.to_dict()
        assert NodeSpecs.from_dict(d) == original

    def test_from_dict_accepts_null_temp(self) -> None:
        s = NodeSpecs.from_dict(
            {
                "model_name": "MacBookPro18,3",
                "os": "darwin",
                "arch": "arm64",
                "cpu_cores": 10,
                "ram_total_bytes": 16 * 1024**3,
                "disk_total_bytes": 512 * 1024**3,
                "cpu_percent": 5.0,
                "mem_used_bytes": 8 * 1024**3,
                "disk_used_bytes": 200 * 1024**3,
                "temp_celsius": None,
                "uptime_seconds": 3600,
                "loadavg_1m": 1.0,
                "loadavg_5m": 0.8,
                "loadavg_15m": 0.7,
            }
        )
        assert s.temp_celsius is None
        assert s.model_name == "MacBookPro18,3"

    def test_from_dict_tolerates_missing_static_fields(self) -> None:
        # Older v1 payloads have only cpu_percent / temp_celsius. The
        # parser must not raise — missing fields surface as "unknown" / 0.
        s = NodeSpecs.from_dict({"cpu_percent": 5.0, "temp_celsius": None})
        assert s.cpu_percent == 5.0
        assert s.temp_celsius is None
        assert s.model_name == "unknown"
        assert s.cpu_cores == 0
        assert s.ram_total_bytes == 0


class TestCollectSpecsMac:
    """No temperature sensor path, em-dash in the UI; sysctl for model name."""

    def test_darwin_returns_none_temp_and_sysctl_model(self) -> None:
        sysctl_result = SimpleNamespace(returncode=0, stdout="MacBookPro18,3\n", stderr="")
        with (
            patch("turing.specs.collector.platform.system", return_value="Darwin"),
            patch("turing.specs.collector.platform.machine", return_value="arm64"),
            patch("turing.specs.collector.psutil.cpu_percent", return_value=23.4),
            patch("turing.specs.collector.psutil.cpu_count", return_value=10),
            patch(
                "turing.specs.collector.psutil.virtual_memory",
                return_value=_vm(used=8 * 1024**3, total=16 * 1024**3),
            ),
            patch(
                "turing.specs.collector.psutil.disk_usage",
                return_value=_du(used=200 * 1024**3, total=512 * 1024**3),
            ),
            patch("turing.specs.collector.psutil.boot_time", return_value=0.0),
            patch("turing.specs.collector.os.getloadavg", return_value=(1.0, 0.8, 0.7)),
            patch("turing.specs.collector.subprocess.run", return_value=sysctl_result),
        ):
            specs = collect_specs()
        assert specs.os == "darwin"
        assert specs.arch == "arm64"
        assert specs.cpu_cores == 10
        assert specs.ram_total_bytes == 16 * 1024**3
        assert specs.disk_total_bytes == 512 * 1024**3
        assert specs.mem_used_bytes == 8 * 1024**3
        assert specs.disk_used_bytes == 200 * 1024**3
        assert specs.model_name == "MacBookPro18,3"
        assert specs.temp_celsius is None
        assert specs.cpu_percent == 23.4
        assert specs.loadavg_1m == 1.0


class TestCollectSpecsPi:
    """gamma-Pi -- single thermal zone + device-tree model + psutil RAM/cores."""

    def test_linux_pi_full_schema(self) -> None:
        device_tree_model = "Raspberry Pi 5 Model B Rev 1.0\x00"
        with (
            patch("turing.specs.collector.platform.system", return_value="Linux"),
            patch("turing.specs.collector.platform.machine", return_value="aarch64"),
            patch("turing.specs.collector.psutil.cpu_percent", return_value=42.0),
            patch("turing.specs.collector.psutil.cpu_count", return_value=4),
            patch(
                "turing.specs.collector.psutil.virtual_memory",
                return_value=_vm(used=2 * 1024**3, total=8 * 1024**3),
            ),
            patch(
                "turing.specs.collector.psutil.disk_usage",
                return_value=_du(used=10 * 1024**3, total=128 * 1024**3),
            ),
            patch("turing.specs.collector.psutil.boot_time", return_value=0.0),
            patch("turing.specs.collector.os.getloadavg", return_value=(0.5, 0.4, 0.3)),
            patch("turing.specs.collector._is_jetson", return_value=False),
            patch(
                "turing.specs.collector.open",
                mock_open(read_data=device_tree_model),
            ),
            # Patch the thermal-zone read in isolation since open() is patched
            # at module scope.
            patch("turing.specs.collector._read_pi_temp", return_value=55.421),
        ):
            specs = collect_specs()
        assert specs.model_name == "Raspberry Pi 5 Model B Rev 1.0"
        assert specs.os == "linux"
        assert specs.arch == "aarch64"
        assert specs.cpu_cores == 4
        assert specs.ram_total_bytes == 8 * 1024**3
        assert specs.disk_total_bytes == 128 * 1024**3
        assert specs.cpu_percent == 42.0
        assert specs.mem_used_bytes == 2 * 1024**3
        assert specs.disk_used_bytes == 10 * 1024**3
        assert specs.temp_celsius is not None
        assert abs(specs.temp_celsius - 55.421) < 1e-6
        assert specs.loadavg_1m == 0.5

    def test_linux_pi_unreadable_temp_zone_returns_none(self) -> None:
        with (
            patch("turing.specs.collector.platform.system", return_value="Linux"),
            patch("turing.specs.collector.platform.machine", return_value="aarch64"),
            patch("turing.specs.collector.psutil.cpu_percent", return_value=42.0),
            patch("turing.specs.collector.psutil.cpu_count", return_value=4),
            patch(
                "turing.specs.collector.psutil.virtual_memory",
                return_value=_vm(used=0, total=8 * 1024**3),
            ),
            patch(
                "turing.specs.collector.psutil.disk_usage",
                return_value=_du(used=0, total=128 * 1024**3),
            ),
            patch("turing.specs.collector.psutil.boot_time", return_value=0.0),
            patch("turing.specs.collector.os.getloadavg", return_value=(0.0, 0.0, 0.0)),
            patch("turing.specs.collector._is_jetson", return_value=False),
            # Model-name reader and temp reader both fail.
            patch("turing.specs.collector._read_linux_model_name", return_value="unknown"),
            patch("turing.specs.collector._read_pi_temp", return_value=None),
        ):
            specs = collect_specs()
        assert specs.temp_celsius is None
        assert specs.model_name == "unknown"


class TestCollectSpecsJetson:
    """gamma-Jetson -- max(zones) across all thermal_zone* entries."""

    def test_jetson_multi_zone_returns_max(self) -> None:
        zones = [
            "/sys/class/thermal/thermal_zone0/temp",
            "/sys/class/thermal/thermal_zone1/temp",
            "/sys/class/thermal/thermal_zone2/temp",
        ]
        readings = {
            zones[0]: "48123\n",
            zones[1]: "61450\n",
            zones[2]: "44000\n",
        }

        def fake_open(path: str, *args: object, **kwargs: object) -> object:
            return mock_open(read_data=readings[path]).return_value

        with (
            patch("turing.specs.collector.platform.system", return_value="Linux"),
            patch("turing.specs.collector.platform.machine", return_value="aarch64"),
            patch("turing.specs.collector.psutil.cpu_percent", return_value=15.0),
            patch("turing.specs.collector.psutil.cpu_count", return_value=6),
            patch(
                "turing.specs.collector.psutil.virtual_memory",
                return_value=_vm(used=3 * 1024**3, total=8 * 1024**3),
            ),
            patch(
                "turing.specs.collector.psutil.disk_usage",
                return_value=_du(used=30 * 1024**3, total=256 * 1024**3),
            ),
            patch("turing.specs.collector.psutil.boot_time", return_value=0.0),
            patch("turing.specs.collector.os.getloadavg", return_value=(2.0, 1.5, 1.0)),
            patch("turing.specs.collector._is_jetson", return_value=True),
            patch("turing.specs.collector.glob.glob", return_value=zones),
            patch("turing.specs.collector.open", side_effect=fake_open),
            patch(
                "turing.specs.collector._read_linux_model_name",
                return_value="NVIDIA Jetson Orin Nano Developer Kit",
            ),
        ):
            specs = collect_specs()
        assert specs.cpu_cores == 6
        assert specs.model_name == "NVIDIA Jetson Orin Nano Developer Kit"
        assert specs.temp_celsius is not None
        assert abs(specs.temp_celsius - 61.450) < 1e-6

    def test_jetson_filters_negative_sentinel_zones(self) -> None:
        zones = ["/sys/class/thermal/thermal_zone0/temp", "/sys/class/thermal/thermal_zone1/temp"]
        readings = {zones[0]: "-100000\n", zones[1]: "55000\n"}

        def fake_open(path: str, *args: object, **kwargs: object) -> object:
            return mock_open(read_data=readings[path]).return_value

        with (
            patch("turing.specs.collector.platform.system", return_value="Linux"),
            patch("turing.specs.collector.platform.machine", return_value="aarch64"),
            patch("turing.specs.collector.psutil.cpu_percent", return_value=5.0),
            patch("turing.specs.collector.psutil.cpu_count", return_value=6),
            patch(
                "turing.specs.collector.psutil.virtual_memory",
                return_value=_vm(used=0, total=8 * 1024**3),
            ),
            patch(
                "turing.specs.collector.psutil.disk_usage",
                return_value=_du(used=0, total=256 * 1024**3),
            ),
            patch("turing.specs.collector.psutil.boot_time", return_value=0.0),
            patch("turing.specs.collector.os.getloadavg", return_value=(0.0, 0.0, 0.0)),
            patch("turing.specs.collector._is_jetson", return_value=True),
            patch("turing.specs.collector.glob.glob", return_value=zones),
            patch("turing.specs.collector.open", side_effect=fake_open),
            patch(
                "turing.specs.collector._read_linux_model_name",
                return_value="jetson",
            ),
        ):
            specs = collect_specs()
        assert specs.temp_celsius == 55.0

    def test_jetson_no_zones_returns_none(self) -> None:
        with (
            patch("turing.specs.collector.platform.system", return_value="Linux"),
            patch("turing.specs.collector.platform.machine", return_value="aarch64"),
            patch("turing.specs.collector.psutil.cpu_percent", return_value=5.0),
            patch("turing.specs.collector.psutil.cpu_count", return_value=6),
            patch(
                "turing.specs.collector.psutil.virtual_memory",
                return_value=_vm(used=0, total=8 * 1024**3),
            ),
            patch(
                "turing.specs.collector.psutil.disk_usage",
                return_value=_du(used=0, total=256 * 1024**3),
            ),
            patch("turing.specs.collector.psutil.boot_time", return_value=0.0),
            patch("turing.specs.collector.os.getloadavg", return_value=(0.0, 0.0, 0.0)),
            patch("turing.specs.collector._is_jetson", return_value=True),
            patch("turing.specs.collector.glob.glob", return_value=[]),
            patch(
                "turing.specs.collector._read_linux_model_name",
                return_value="jetson",
            ),
        ):
            specs = collect_specs()
        assert specs.temp_celsius is None


class TestStaticFieldsCached:
    """Static fields sampled once on first call, reused thereafter."""

    def test_second_call_does_not_resample_static(self) -> None:
        sysctl_result = SimpleNamespace(returncode=0, stdout="MacBookPro18,3\n", stderr="")
        with (
            patch("turing.specs.collector.platform.system", return_value="Darwin"),
            patch("turing.specs.collector.platform.machine", return_value="arm64"),
            patch("turing.specs.collector.psutil.cpu_percent", return_value=10.0),
            patch("turing.specs.collector.psutil.cpu_count", return_value=10) as cpu_count_mock,
            patch(
                "turing.specs.collector.psutil.virtual_memory",
                return_value=_vm(used=0, total=16 * 1024**3),
            ),
            patch(
                "turing.specs.collector.psutil.disk_usage",
                return_value=_du(used=0, total=512 * 1024**3),
            ),
            patch("turing.specs.collector.psutil.boot_time", return_value=0.0),
            patch("turing.specs.collector.os.getloadavg", return_value=(0.0, 0.0, 0.0)),
            patch(
                "turing.specs.collector.subprocess.run", return_value=sysctl_result
            ) as sysctl_mock,
        ):
            collect_specs()
            collect_specs()
            collect_specs()
        # ``cpu_count`` and ``sysctl`` are part of the static block; they
        # must be invoked exactly once across the three calls.
        assert cpu_count_mock.call_count == 1
        assert sysctl_mock.call_count == 1


class TestModelNameUnknownPlatform:
    def test_unknown_system_returns_unknown_model(self) -> None:
        with (
            patch("turing.specs.collector.platform.system", return_value="Plan9"),
            patch("turing.specs.collector.platform.machine", return_value="weird"),
            patch("turing.specs.collector.psutil.cpu_percent", return_value=0.0),
            patch("turing.specs.collector.psutil.cpu_count", return_value=1),
            patch(
                "turing.specs.collector.psutil.virtual_memory",
                return_value=_vm(used=0, total=1024**3),
            ),
            patch(
                "turing.specs.collector.psutil.disk_usage",
                return_value=_du(used=0, total=1024**3),
            ),
            patch("turing.specs.collector.psutil.boot_time", return_value=0.0),
            patch("turing.specs.collector.os.getloadavg", return_value=(0.0, 0.0, 0.0)),
        ):
            specs = collect_specs()
        assert specs.model_name == "unknown"
        assert specs.os == "plan9"
        assert specs.temp_celsius is None
