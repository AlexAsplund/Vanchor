"""Named boat profiles (#75): the persistent store, the Runtime live-apply, and
the REST surface."""

import pytest
from fastapi.testclient import TestClient

from vanchor.app import Runtime
from vanchor.core.boat_profiles import BoatProfileStore, specs_from_boat
from vanchor.core.config import AppConfig, BoatConfig
from vanchor.ui.server import create_app


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
def test_seeds_starter_presets(tmp_path):
    # First run with no explicit seed -> the ready-to-pick presets (#89), with
    # the bow trolling motor active.
    store = BoatProfileStore(str(tmp_path))
    profiles = store.list()
    ids = [p["id"] for p in profiles]
    assert ids == [
        "bow-trolling-motor",
        "stern-trolling-motor",
        "off-centre-bow-trolling",
        "jon-boat-flat-bottom",
        "15-hp-stern-outboard",
    ]
    assert store.active_id == "bow-trolling-motor"
    # The flat list shape carries the spec fields inline.
    assert "max_speed_mps" in profiles[0]


def test_preset_specs_differ_realistically(tmp_path):
    store = BoatProfileStore(str(tmp_path))
    bow = store.get("bow-trolling-motor")["specs"]
    stern = store.get("stern-trolling-motor")["specs"]
    offcentre = store.get("off-centre-bow-trolling")["specs"]
    outboard = store.get("15-hp-stern-outboard")["specs"]
    # Bow motor mirrors the BoatConfig defaults (a fresh install is unchanged).
    assert bow["thruster_mount"] == "bow"
    assert bow["max_thrust_n"] == BoatConfig().max_thrust_n
    # Stern mount yaws/steers the opposite way.
    assert stern["thruster_mount"] == "stern"
    # Off-centre bow gives the thrust-yaw feed-forward something to cancel.
    assert offcentre["thruster_mount"] == "bow"
    assert offcentre["thruster_y_m"] > 0.0
    # The outboard is a much faster, more powerful, heavier boat.
    assert outboard["thruster_mount"] == "stern"
    assert outboard["max_thrust_n"] > bow["max_thrust_n"]
    assert outboard["max_speed_mps"] > bow["max_speed_mps"]
    assert outboard["mass_kg"] > bow["mass_kg"]
    # Hull character / tracking spans a real range: the jon boat is loose, the
    # outboard tracks more than the default skiff.
    jon = store.get("jon-boat-flat-bottom")["specs"]
    assert jon["hull_tracking"] < bow["hull_tracking"] == 1.0
    assert outboard["hull_tracking"] > bow["hull_tracking"]


def test_seed_from_given_boat(tmp_path):
    store = BoatProfileStore(str(tmp_path), seed=BoatConfig(max_speed_mps=2.4, mass_kg=410.0))
    active = store.active()
    assert active["specs"]["max_speed_mps"] == 2.4
    assert active["specs"]["mass_kg"] == 410.0


def test_create_list_get(tmp_path):
    store = BoatProfileStore(str(tmp_path))
    n0 = len(store.list())
    pid = store.create("Light Kayak", {"mass_kg": 120.0})
    assert pid == "light-kayak"
    assert len(store.list()) == n0 + 1
    prof = store.get(pid)
    assert prof["name"] == "Light Kayak"
    assert prof["specs"]["mass_kg"] == 120.0
    # Unspecified fields fall back to BoatConfig defaults (profile is complete).
    assert prof["specs"]["beam_m"] == BoatConfig().beam_m


def test_ids_are_deterministic_on_collision(tmp_path):
    store = BoatProfileStore(str(tmp_path))
    a = store.create("Skiff")
    b = store.create("Skiff")
    assert a == "skiff"
    assert b != a and b.startswith("skiff")
    # No wall-clock in the id.
    assert all(part.isalnum() or part == "-" for part in b)


def test_save_updates_name_and_specs(tmp_path):
    store = BoatProfileStore(str(tmp_path))
    pid = store.create("Skiff", {"mass_kg": 300.0})
    assert store.save(pid, "Renamed", {"mass_kg": 333.0})
    prof = store.get(pid)
    assert prof["name"] == "Renamed"
    assert prof["specs"]["mass_kg"] == 333.0
    assert store.save("nope", "x", None) is False


def test_set_active(tmp_path):
    store = BoatProfileStore(str(tmp_path))
    pid = store.create("Skiff")
    assert store.set_active(pid)
    assert store.active_id == pid
    assert store.set_active("missing") is False


def test_delete_and_cannot_delete_last(tmp_path):
    # Use a single-seed store so we can drain it down to the last profile.
    store = BoatProfileStore(str(tmp_path), seed=BoatConfig())
    pid = store.create("Skiff")
    assert store.delete(pid)
    assert len(store.list()) == 1
    # Now only "default" remains; refuse to delete it.
    assert store.delete("default") is False
    assert len(store.list()) == 1


def test_delete_active_falls_back(tmp_path):
    store = BoatProfileStore(str(tmp_path))
    first = store.active_id
    pid = store.create("Skiff")
    store.set_active(pid)
    assert store.delete(pid)
    # Falls back to the first remaining profile.
    assert store.active_id == first


def test_persistence_round_trips(tmp_path):
    store = BoatProfileStore(str(tmp_path))
    seeded = {p["id"] for p in store.list()}
    pid = store.create("Skiff", {"max_speed_mps": 2.1})
    store.set_active(pid)
    # A fresh store over the same dir must see the same data + active selection.
    reloaded = BoatProfileStore(str(tmp_path))
    assert reloaded.active_id == pid
    assert reloaded.get(pid)["specs"]["max_speed_mps"] == 2.1
    assert {p["id"] for p in reloaded.list()} == seeded | {pid}


# --------------------------------------------------------------------------- #
# Runtime live-apply
# --------------------------------------------------------------------------- #
def _runtime(tmp_path) -> Runtime:
    cfg = AppConfig()
    cfg.data_dir = str(tmp_path)
    return Runtime(cfg)


def test_activate_applies_specs_to_boat_profile(tmp_path):
    rt = _runtime(tmp_path)
    pid = rt.boat_profiles_create("Fast", {"max_speed_mps": 3.3})["id"]
    applied = rt.boat_profiles_activate(pid)
    # The active profile's spec is reflected in the live boat profile.
    assert applied["max_speed_mps"] == 3.3
    assert applied["active_boat_id"] == pid
    assert rt.boat_profile()["max_speed_mps"] == 3.3
    assert rt.config.boat.max_speed_mps == 3.3


def test_activate_rebuilds_live_physics(tmp_path):
    rt = _runtime(tmp_path)
    pid = rt.boat_profiles_create("Fast", {"max_speed_mps": 3.5, "mass_kg": 500.0})["id"]
    rt.boat_profiles_activate(pid)
    # The simulator's physics params were rebuilt, not just the telemetry.
    params = rt.simulator.boat.params
    assert params.max_speed_mps == 3.5
    assert params.mass == 500.0
    # Fossen derives mass-dependent yaw inertia at (re)build time.
    assert params.iz == pytest.approx(500.0 / 12.0 * (params.length**2 + params.beam**2))


def test_activate_stern_preset_flips_geometry(tmp_path):
    rt = _runtime(tmp_path)
    rt.boat_profiles_activate("stern-trolling-motor")
    # A stern mount sits behind the CG: negative longitudinal offset.
    assert rt.config.boat.thruster_mount == "stern"
    assert rt.config.boat.thruster_x_m() < 0
    # The helm steer-sign flips so the autopilot still turns the right way.
    assert rt.controller.helm.steer_sign == -1.0


def test_activate_outboard_preset_higher_thrust_speed(tmp_path):
    rt = _runtime(tmp_path)
    bow = rt.boats.get("bow-trolling-motor")["specs"]
    applied = rt.boat_profiles_activate("15-hp-stern-outboard")
    assert applied["max_thrust_n"] > bow["max_thrust_n"]
    assert applied["max_speed_mps"] > bow["max_speed_mps"]
    # Live physics rebuilt with the bigger, faster, heavier boat.
    params = rt.simulator.boat.params
    assert params.max_thrust_n == applied["max_thrust_n"]
    assert params.max_speed_mps == applied["max_speed_mps"]
    assert params.mass == applied["mass_kg"]
    # Stern mount -> thruster behind the CG.
    assert params.thruster_x_m < 0


def test_activate_unknown_returns_none(tmp_path):
    rt = _runtime(tmp_path)
    assert rt.boat_profiles_activate("nope") is None


def test_update_active_applies_live(tmp_path):
    rt = _runtime(tmp_path)
    active = rt.boats.active_id
    rt.boat_profiles_update(active, specs={"max_speed_mps": 2.7})
    assert rt.config.boat.max_speed_mps == 2.7
    assert rt.simulator.boat.params.max_speed_mps == 2.7


def test_update_boat_writes_back_to_active_profile(tmp_path):
    rt = _runtime(tmp_path)
    active = rt.boats.active_id
    rt.update_boat({"max_speed_mps": 1.95})
    # The POST /api/boat path persisted into the active profile.
    assert rt.boats.get(active)["specs"]["max_speed_mps"] == 1.95
    # And a reloaded store sees it.
    reloaded = BoatProfileStore(str(tmp_path))
    assert reloaded.get(active)["specs"]["max_speed_mps"] == 1.95


def test_active_selection_applied_on_restart(tmp_path):
    rt = _runtime(tmp_path)
    pid = rt.boat_profiles_create("Heavy", {"mass_kg": 600.0})["id"]
    rt.boat_profiles_activate(pid)
    # A brand-new Runtime over the same data_dir applies the saved active profile.
    rt2 = _runtime(tmp_path)
    assert rt2.boats.active_id == pid
    assert rt2.config.boat.mass_kg == 600.0
    assert rt2.simulator.boat.params.mass == 600.0


# --------------------------------------------------------------------------- #
# REST
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(tmp_path):
    cfg = AppConfig()
    cfg.data_dir = str(tmp_path)
    app = create_app(Runtime(cfg))
    with TestClient(app) as c:
        yield c


def test_rest_list_create_activate(client):
    listing = client.get("/api/boat/profiles").json()
    assert listing["active_id"] == "bow-trolling-motor"
    assert listing["profiles"][0]["id"] == "bow-trolling-motor"

    created = client.post("/api/boat/profiles", json={"name": "Cruiser", "specs": {"max_speed_mps": 2.8}}).json()
    pid = created["id"]
    assert created["specs"]["max_speed_mps"] == 2.8

    applied = client.post(f"/api/boat/profiles/{pid}/activate").json()
    assert applied["max_speed_mps"] == 2.8
    assert applied["active_boat_id"] == pid
    # Telemetry now reflects the activated profile.
    state = client.get("/api/state").json()
    assert state["boat"]["max_speed_mps"] == 2.8
    assert state["boat"]["active_boat_id"] == pid


def test_rest_create_defaults_to_active_boat(client):
    # No specs -> the new profile copies the current active boat's specs.
    cur = client.get("/api/boat").json()
    created = client.post("/api/boat/profiles", json={"name": "Copy"}).json()
    assert created["specs"]["max_speed_mps"] == cur["max_speed_mps"]


def test_rest_update(client):
    pid = client.post("/api/boat/profiles", json={"name": "X"}).json()["id"]
    updated = client.post(f"/api/boat/profiles/{pid}", json={"name": "Y", "specs": {"mass_kg": 222.0}}).json()
    assert updated["name"] == "Y"
    assert updated["specs"]["mass_kg"] == 222.0
    assert client.post("/api/boat/profiles/missing", json={"name": "Z"}).status_code == 404


def test_rest_delete_and_refuse_last(client):
    pid = client.post("/api/boat/profiles", json={"name": "Temp"}).json()["id"]
    assert client.delete(f"/api/boat/profiles/{pid}").json() == {"ok": True}
    # Drain every seeded preset but one.
    listing = client.get("/api/boat/profiles").json()["profiles"]
    for prof in listing[:-1]:
        assert client.delete(f"/api/boat/profiles/{prof['id']}").json() == {"ok": True}
    # One profile left -> refuse to delete it.
    last = client.get("/api/boat/profiles").json()["profiles"][0]["id"]
    assert client.delete(f"/api/boat/profiles/{last}").json() == {"ok": False}


def test_rest_activate_unknown_404(client):
    assert client.post("/api/boat/profiles/nope/activate").status_code == 404
