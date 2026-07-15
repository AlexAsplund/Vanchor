"""Tests for the nested, file-backed application configuration."""

from __future__ import annotations

import json

import pytest

from vanchor.core.config import (
    AppConfig,
    BoatConfig,
    ControlConfig,
    DEFAULT_CONFIG_YAML,
    HardwareConfig,
    NmeaTcpConfig,
    SensorConfig,
    ServerConfig,
    SimConfig,
    apply_env_overrides,
    load,
    load_dotenv,
)


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every VANCHOR_* var so each env test starts from a clean slate and
    restore the original environment afterwards.

    ``load_dotenv`` writes to ``os.environ`` directly (via ``setdefault``),
    bypassing monkeypatch's tracking, so we snapshot the VANCHOR_* keys and
    clear any that the test introduced on teardown -- otherwise a value like
    ``VANCHOR_DATA_DIR`` would leak into later tests that build a Runtime.
    """
    import os as _os

    saved = {k: v for k, v in _os.environ.items() if k.startswith("VANCHOR_")}
    for key in saved:
        monkeypatch.delenv(key, raising=False)
    yield monkeypatch
    for key in [k for k in _os.environ if k.startswith("VANCHOR_")]:
        del _os.environ[key]
    _os.environ.update(saved)


def test_boat_thruster_offset_signs() -> None:
    bow = BoatConfig(length_m=4.1, thruster_mount="bow").thruster_x_m()
    stern = BoatConfig(length_m=4.1, thruster_mount="stern").thruster_x_m()
    assert bow > 0 and stern < 0
    # The CG sits aft of centre, so a bow motor's lever arm is LONGER than a
    # stern motor's (the heart of the "turns sharper" effect).
    assert bow > abs(stern)
    # With the CG at the geometric centre (cg_aft_frac=0) a centre mount has no arm.
    assert BoatConfig(thruster_mount="center", cg_aft_frac=0.0).thruster_x_m() == 0.0
    # A further-aft CG lengthens a bow motor's arm (sharper turn).
    base = BoatConfig(length_m=4.1, thruster_mount="bow", cg_aft_frac=0.0).thruster_x_m()
    assert BoatConfig(length_m=4.1, thruster_mount="bow", cg_aft_frac=0.2).thruster_x_m() > base
    # An explicit offset overrides the mount keyword (CG already encoded in it).
    assert BoatConfig(thruster_mount="bow", thruster_offset_m=-0.9).thruster_x_m() == -0.9


def test_boat_config_in_tree_and_roundtrip() -> None:
    cfg = AppConfig.from_dict({"boat": {"length_m": 5.5, "thruster_mount": "stern"}})
    assert cfg.boat.length_m == 5.5
    assert cfg.boat.thruster_mount == "stern"
    assert AppConfig.from_dict(cfg.to_dict()).boat.length_m == 5.5


def test_defaults_with_no_file() -> None:
    cfg = load(None)
    assert isinstance(cfg, AppConfig)
    assert cfg.sim.start_lat == SimConfig().start_lat
    assert cfg.sim.physics_hz == 20.0
    assert cfg.sensors.gps_hz == 10.0  # matches the M9N marine config
    assert cfg.control.tick_hz == 5.0
    assert cfg.control.heading_kd == 0.012
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 8000
    assert cfg.hardware.enabled is False
    assert cfg.nmea_tcp.port == 10110


def test_missing_file_returns_defaults(tmp_path) -> None:
    cfg = load(tmp_path / "does-not-exist.yaml")
    assert cfg == AppConfig()


def test_from_dict_none_is_defaults() -> None:
    assert AppConfig.from_dict(None) == AppConfig()
    assert AppConfig.from_dict({}) == AppConfig()


def test_from_dict_partial_override_only_touches_given_fields() -> None:
    cfg = AppConfig.from_dict(
        {"control": {"tick_hz": 10.0}, "server": {"port": 9001}}
    )
    # Overridden fields.
    assert cfg.control.tick_hz == 10.0
    assert cfg.server.port == 9001
    # Untouched sibling fields keep defaults.
    assert cfg.control.heading_kp == ControlConfig().heading_kp
    assert cfg.control.anchor_radius_m == ControlConfig().anchor_radius_m
    assert cfg.server.host == ServerConfig().host
    # Untouched sub-configs entirely default.
    assert cfg.sim == SimConfig()
    assert cfg.sensors == SensorConfig()


def test_from_dict_ignores_unknown_keys() -> None:
    cfg = AppConfig.from_dict(
        {
            "sim": {"start_lat": 1.0, "bogus": 123},
            "totally_unknown_section": {"x": 1},
        }
    )
    assert cfg.sim.start_lat == 1.0
    assert cfg.sim.start_lon == SimConfig().start_lon
    assert not hasattr(cfg, "totally_unknown_section")


def test_to_dict_from_dict_roundtrip() -> None:
    cfg = AppConfig.from_dict(
        {
            "sim": {"start_lat": 12.34, "time_scale": 4.0},
            "environment": {"wind_speed": 3.0, "wind_dir": 270.0},
            "control": {"anchor_kp": 0.1, "waypoint_throttle": 0.9},
            "hardware": {"enabled": True, "baudrate": 38400},
        }
    )
    d = cfg.to_dict()
    assert isinstance(d, dict)
    assert d["sim"]["start_lat"] == 12.34
    assert AppConfig.from_dict(d) == cfg


def test_yaml_file_loads(tmp_path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(
        "sim:\n"
        "  physics_hz: 50.0\n"
        "control:\n"
        "  tick_hz: 8.0\n"
        "hardware:\n"
        "  enabled: true\n"
        "  motor_port: /dev/ttyACM0\n",
        encoding="utf-8",
    )
    cfg = load(p)
    assert cfg.sim.physics_hz == 50.0
    assert cfg.control.tick_hz == 8.0
    assert cfg.hardware.enabled is True
    assert cfg.hardware.motor_port == "/dev/ttyACM0"
    # Untouched defaults remain.
    assert cfg.sensors == SensorConfig()


def test_yml_extension_loads(tmp_path) -> None:
    p = tmp_path / "config.yml"
    p.write_text("server:\n  port: 7777\n", encoding="utf-8")
    cfg = load(p)
    assert cfg.server.port == 7777


def test_json_file_loads(tmp_path) -> None:
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps(
            {
                "sensors": {"gps_hz": 2.0, "compass_hz": 10.0},
                "server": {"host": "0.0.0.0", "port": 8080},
            }
        ),
        encoding="utf-8",
    )
    cfg = load(p)
    assert cfg.sensors.gps_hz == 2.0
    assert cfg.sensors.compass_hz == 10.0
    assert cfg.server.host == "0.0.0.0"
    assert cfg.server.port == 8080


def test_load_string_path(tmp_path) -> None:
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    cfg = load(str(p))
    assert cfg == AppConfig()


def test_unsupported_extension_raises(tmp_path) -> None:
    p = tmp_path / "config.toml"
    p.write_text("nope", encoding="utf-8")
    with pytest.raises(ValueError):
        load(p)


def test_non_mapping_top_level_raises(tmp_path) -> None:
    p = tmp_path / "config.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError):
        load(p)


def test_default_config_yaml_parses_to_defaults(tmp_path) -> None:
    p = tmp_path / "default.yaml"
    p.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
    cfg = load(p)
    assert cfg == AppConfig()


# --- Environment binding ------------------------------------------------- #


def test_nmea_tcp_has_host_default() -> None:
    assert NmeaTcpConfig().host == "0.0.0.0"


def test_apply_env_overrides_scalars(clean_env) -> None:
    clean_env.setenv("VANCHOR_PORT", "9999")
    clean_env.setenv("VANCHOR_DATA_DIR", "/srv/vanchor")
    clean_env.setenv("VANCHOR_HOST", "0.0.0.0")
    clean_env.setenv("VANCHOR_PHYSICS_HZ", "33.0")
    cfg = apply_env_overrides(AppConfig())
    assert cfg.server.port == 9999
    assert isinstance(cfg.server.port, int)
    assert cfg.data_dir == "/srv/vanchor"
    assert cfg.server.host == "0.0.0.0"
    assert cfg.sim.physics_hz == 33.0


def test_apply_env_overrides_bools(clean_env) -> None:
    clean_env.setenv("VANCHOR_HARDWARE", "yes")
    clean_env.setenv("VANCHOR_NMEA_TCP", "on")
    cfg = apply_env_overrides(AppConfig())
    assert cfg.hardware.enabled is True
    assert cfg.nmea_tcp.enabled is True
    # And a clearly-false value disables.
    clean_env.setenv("VANCHOR_HARDWARE", "0")
    assert apply_env_overrides(AppConfig()).hardware.enabled is False


def test_apply_env_overrides_unset_keeps_defaults(clean_env) -> None:
    cfg = apply_env_overrides(AppConfig())
    assert cfg == AppConfig()


def test_apply_env_overrides_full_surface(clean_env) -> None:
    env = {
        "VANCHOR_MODEL": "simple",
        "VANCHOR_TIME_SCALE": "4.0",
        "VANCHOR_SIM_START_LAT": "1.5",
        "VANCHOR_SIM_START_LON": "2.5",
        "VANCHOR_GPS_PORT": "/dev/ttyACM0",
        "VANCHOR_COMPASS_PORT": "/dev/ttyACM1",
        "VANCHOR_MOTOR_PORT": "/dev/ttyACM2",
        "VANCHOR_BAUDRATE": "38400",
        "VANCHOR_GPS_SOURCE": "nmea",
        "VANCHOR_COMPASS_SOURCE": "serial",
        "VANCHOR_DEPTH_SOURCE": "nmea",
        "VANCHOR_MOTOR_SOURCE": "both",
        "VANCHOR_NMEA_TCP_HOST": "127.0.0.1",
        "VANCHOR_NMEA_TCP_PORT": "10120",
    }
    for k, v in env.items():
        clean_env.setenv(k, v)
    cfg = apply_env_overrides(AppConfig())
    assert cfg.sim.model == "simple"
    assert cfg.sim.time_scale == 4.0
    assert cfg.sim.start_lat == 1.5
    assert cfg.sim.start_lon == 2.5
    assert cfg.hardware.gps_port == "/dev/ttyACM0"
    assert cfg.hardware.compass_port == "/dev/ttyACM1"
    assert cfg.hardware.motor_port == "/dev/ttyACM2"
    assert cfg.hardware.baudrate == 38400
    assert cfg.hardware.gps_source == "nmea"
    assert cfg.hardware.compass_source == "serial"
    assert cfg.hardware.depth_source == "nmea"
    assert cfg.hardware.motor_source == "both"
    assert cfg.nmea_tcp.host == "127.0.0.1"
    assert cfg.nmea_tcp.port == 10120


def test_env_overrides_win_over_yaml(clean_env, tmp_path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("server:\n  port: 7000\n", encoding="utf-8")
    clean_env.setenv("VANCHOR_PORT", "8123")
    cfg = load(p)
    assert cfg.server.port == 8123


def test_load_with_no_file_applies_env(clean_env) -> None:
    clean_env.setenv("VANCHOR_PORT", "8456")
    cfg = load(None)
    assert cfg.server.port == 8456


def test_load_dotenv_reads_file_and_sets_env(clean_env, tmp_path) -> None:
    p = tmp_path / ".env"
    p.write_text(
        "# a comment\n"
        "\n"
        "VANCHOR_PORT=8765\n"
        'VANCHOR_DATA_DIR="/quoted/path"\n'
        "export VANCHOR_HOST=0.0.0.0\n",
        encoding="utf-8",
    )
    load_dotenv(p)
    import os as _os

    assert _os.environ["VANCHOR_PORT"] == "8765"
    assert _os.environ["VANCHOR_DATA_DIR"] == "/quoted/path"
    assert _os.environ["VANCHOR_HOST"] == "0.0.0.0"
    cfg = apply_env_overrides(AppConfig())
    assert cfg.server.port == 8765
    assert cfg.data_dir == "/quoted/path"


def test_real_env_beats_dotenv(clean_env, tmp_path) -> None:
    p = tmp_path / ".env"
    p.write_text("VANCHOR_PORT=1111\n", encoding="utf-8")
    clean_env.setenv("VANCHOR_PORT", "2222")  # real env set first
    load_dotenv(p)
    import os as _os

    assert _os.environ["VANCHOR_PORT"] == "2222"  # not clobbered by .env


def test_load_dotenv_via_env_file_var(clean_env, tmp_path) -> None:
    p = tmp_path / "custom.env"
    p.write_text("VANCHOR_PORT=3333\n", encoding="utf-8")
    clean_env.setenv("VANCHOR_ENV_FILE", str(p))
    cfg = load(None)
    assert cfg.server.port == 3333


def test_load_dotenv_missing_file_is_noop(clean_env, tmp_path) -> None:
    assert load_dotenv(tmp_path / "nope.env") == {}


# --- Per-device baud rate config ------------------------------------------ #


def test_per_device_baud_defaults() -> None:
    """gps_baud defaults to 38400; compass defaults to 4800 (NMEA standard);
    motor/steering/thrust default to 115200 (protocol v2)."""
    hw = HardwareConfig()
    assert hw.gps_baud == 38400
    assert hw.compass_baud == 4800
    assert hw.motor_baud == 115200
    # The shared fallback stays at 4800 for backward compat.
    assert hw.baudrate == 4800


def test_per_device_baud_round_trips() -> None:
    """Per-device baud keys survive from_dict / to_dict / from_dict round-trip."""
    cfg = AppConfig.from_dict({
        "hardware": {"gps_baud": 115200, "compass_baud": 9600, "motor_baud": 19200}
    })
    assert cfg.hardware.gps_baud == 115200
    assert cfg.hardware.compass_baud == 9600
    assert cfg.hardware.motor_baud == 19200
    # Roundtrip.
    assert AppConfig.from_dict(cfg.to_dict()).hardware.gps_baud == 115200


def test_per_device_baud_independent_of_shared_baudrate() -> None:
    """Setting the shared baudrate does not override per-device keys."""
    cfg = AppConfig.from_dict({"hardware": {"baudrate": 9600}})
    # Per-device keys keep their own defaults unchanged.
    assert cfg.hardware.baudrate == 9600
    assert cfg.hardware.gps_baud == 38400      # GPS default is not 9600
    assert cfg.hardware.compass_baud == 4800   # unchanged
    assert cfg.hardware.motor_baud == 115200   # unchanged from v2 default


def test_per_device_baud_env_overrides(clean_env) -> None:
    clean_env.setenv("VANCHOR_GPS_BAUD", "57600")
    clean_env.setenv("VANCHOR_COMPASS_BAUD", "9600")
    clean_env.setenv("VANCHOR_MOTOR_BAUD", "19200")
    cfg = apply_env_overrides(AppConfig())
    assert cfg.hardware.gps_baud == 57600
    assert cfg.hardware.compass_baud == 9600
    assert cfg.hardware.motor_baud == 19200


def test_default_config_yaml_has_per_device_baud() -> None:
    """DEFAULT_CONFIG_YAML encodes the per-device baud defaults correctly."""
    import io
    import yaml  # type: ignore[import]
    from vanchor.core.config import DEFAULT_CONFIG_YAML
    parsed = yaml.safe_load(io.StringIO(DEFAULT_CONFIG_YAML))
    hw = parsed.get("hardware", {})
    assert hw.get("gps_baud") == 38400
    assert hw.get("compass_baud") == 4800
    assert hw.get("motor_baud") == 115200


def test_water_env_overrides(clean_env) -> None:
    from vanchor.nav import water

    # Defaults when unset.
    assert water.overpass_endpoints() == water.OVERPASS_ENDPOINTS
    assert water.user_agent() == water.USER_AGENT
    assert "your-org" in water.USER_AGENT  # placeholder github URL replaced
    # Overridden at use-time.
    clean_env.setenv(
        "VANCHOR_OVERPASS_URLS", "https://a.example/api, https://b.example/api"
    )
    clean_env.setenv("VANCHOR_USER_AGENT", "custom-agent/1.0")
    assert water.overpass_endpoints() == (
        "https://a.example/api",
        "https://b.example/api",
    )
    assert water.user_agent() == "custom-agent/1.0"
