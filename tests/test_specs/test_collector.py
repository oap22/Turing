"""Tests for :mod:`turing.specs.collector`.

Per-OS branching is the riskiest part of the specs panel feature; this
module covers each branch (Mac, Pi single-zone, Jetson multi-zone) with
``platform.system()`` and filesystem access mocked.
"""

from __future__ import annotations

from unittest.mock import mock_open, patch

from turing.specs.collector import NodeSpecs, collect_specs


class TestNodeSpecsRoundTrip:
    def test_to_dict_and_from_dict(self) -> None:
        original = NodeSpecs(cpu_percent=12.5, temp_celsius=48.0)
        d = original.to_dict()
        assert d == {"cpu_percent": 12.5, "temp_celsius": 48.0}
        assert NodeSpecs.from_dict(d) == original

    def test_from_dict_accepts_null_temp(self) -> None:
        s = NodeSpecs.from_dict({"cpu_percent": 5.0, "temp_celsius": None})
        assert s.temp_celsius is None
        assert s.cpu_percent == 5.0

    def test_from_dict_defaults_missing_cpu_to_zero(self) -> None:
        # Defensive: heartbeat payloads from older nodes shouldn't crash the parser.
        s = NodeSpecs.from_dict({"temp_celsius": None})
        assert s.cpu_percent == 0.0


class TestCollectSpecsMac:
    """Test gamma-Mac -- no temperature sensor path, em-dash in the UI."""

    def test_darwin_returns_none_temp(self) -> None:
        with (
            patch("turing.specs.collector.platform.system", return_value="Darwin"),
            patch("turing.specs.collector.psutil.cpu_percent", return_value=23.4),
        ):
            specs = collect_specs()
        assert specs.cpu_percent == 23.4
        assert specs.temp_celsius is None

    def test_darwin_does_not_open_thermal_zone(self) -> None:
        # Critical: Mac path must not invoke sudo or attempt SMC reads.
        # We assert by ensuring ``open`` is never called on a thermal path.
        with (
            patch("turing.specs.collector.platform.system", return_value="Darwin"),
            patch("turing.specs.collector.psutil.cpu_percent", return_value=10.0),
            patch("turing.specs.collector.open", mock_open(read_data="0")) as m,
        ):
            collect_specs()
        # ``open`` was patched at the module scope but never invoked because
        # the Darwin branch returns early.
        assert m.call_count == 0


class TestCollectSpecsPi:
    def test_linux_pi_single_zone(self) -> None:
        # Pi 4/Pi 5: ``/sys/class/thermal/thermal_zone0/temp`` reports millideg C.
        with (
            patch("turing.specs.collector.platform.system", return_value="Linux"),
            patch("turing.specs.collector.psutil.cpu_percent", return_value=42.0),
            patch("turing.specs.collector._is_jetson", return_value=False),
            patch("turing.specs.collector.open", mock_open(read_data="55421\n")),
        ):
            specs = collect_specs()
        assert specs.cpu_percent == 42.0
        assert specs.temp_celsius is not None
        # 55421 millideg → 55.421 C, allow tiny float wiggle.
        assert abs(specs.temp_celsius - 55.421) < 1e-6

    def test_linux_pi_unreadable_zone_returns_none(self) -> None:
        with (
            patch("turing.specs.collector.platform.system", return_value="Linux"),
            patch("turing.specs.collector.psutil.cpu_percent", return_value=42.0),
            patch("turing.specs.collector._is_jetson", return_value=False),
            patch("turing.specs.collector.open", side_effect=OSError("no such file")),
        ):
            specs = collect_specs()
        assert specs.cpu_percent == 42.0
        assert specs.temp_celsius is None


class TestCollectSpecsJetson:
    def test_jetson_multi_zone_returns_max(self) -> None:
        zones = [
            "/sys/class/thermal/thermal_zone0/temp",  # CPU
            "/sys/class/thermal/thermal_zone1/temp",  # GPU (hotter)
            "/sys/class/thermal/thermal_zone2/temp",  # AO (cooler)
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
            patch("turing.specs.collector.psutil.cpu_percent", return_value=15.0),
            patch("turing.specs.collector._is_jetson", return_value=True),
            patch("turing.specs.collector.glob.glob", return_value=zones),
            patch("turing.specs.collector.open", side_effect=fake_open),
        ):
            specs = collect_specs()
        assert specs.cpu_percent == 15.0
        assert specs.temp_celsius is not None
        assert abs(specs.temp_celsius - 61.450) < 1e-6

    def test_jetson_filters_negative_sentinel_zones(self) -> None:
        zones = ["/sys/class/thermal/thermal_zone0/temp", "/sys/class/thermal/thermal_zone1/temp"]
        readings = {zones[0]: "-100000\n", zones[1]: "55000\n"}

        def fake_open(path: str, *args: object, **kwargs: object) -> object:
            return mock_open(read_data=readings[path]).return_value

        with (
            patch("turing.specs.collector.platform.system", return_value="Linux"),
            patch("turing.specs.collector.psutil.cpu_percent", return_value=5.0),
            patch("turing.specs.collector._is_jetson", return_value=True),
            patch("turing.specs.collector.glob.glob", return_value=zones),
            patch("turing.specs.collector.open", side_effect=fake_open),
        ):
            specs = collect_specs()
        assert specs.temp_celsius == 55.0

    def test_jetson_no_zones_returns_none(self) -> None:
        with (
            patch("turing.specs.collector.platform.system", return_value="Linux"),
            patch("turing.specs.collector.psutil.cpu_percent", return_value=5.0),
            patch("turing.specs.collector._is_jetson", return_value=True),
            patch("turing.specs.collector.glob.glob", return_value=[]),
        ):
            specs = collect_specs()
        assert specs.temp_celsius is None
