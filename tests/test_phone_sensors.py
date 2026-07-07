"""Phone-as-sensor devices: single-feeder arbitration + the GPS/compass pipelines."""
import asyncio

from vanchor.core import events
from vanchor.core.events import EventBus
from vanchor.hardware.drivers.phone import PhoneCompass, PhoneGps, PhoneSensorHub


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _gps_setup():
    bus = EventBus()
    fixes: list = []

    async def grab(fix):
        fixes.append(fix)

    bus.subscribe(events.GPS_FIX_IN, grab)
    hub = PhoneSensorHub(mono_fn=lambda: 100.0)
    dev = PhoneGps(hub, bus)
    return hub, dev, fixes


SAMPLE = {"lat": 59.87, "lon": 12.03, "accuracy": 12.5, "speed": 1.0, "heading": 45.0}


def test_first_client_claims_and_feeds():
    hub, dev, fixes = _gps_setup()

    async def go():
        await dev.start()
        assert await hub.ingest("gps", "A", SAMPLE) == "accepted"
        assert await hub.ingest("gps", "A", SAMPLE) == "accepted"

    _run(go())
    assert len(fixes) == 2
    f = fixes[0]
    assert abs(f.point.lat - 59.87) < 1e-9
    assert f.h_acc_m == 12.5                       # phone accuracy rides the fix
    assert abs(f.sog_knots - 1.9438445) < 1e-3     # 1 m/s
    assert f.cog_deg == 45.0


def test_second_client_is_rejected_while_first_holds():
    hub, dev, fixes = _gps_setup()

    async def go():
        await dev.start()
        assert await hub.ingest("gps", "A", SAMPLE) == "accepted"
        assert await hub.ingest("gps", "B", SAMPLE) == "rejected"
        assert await hub.ingest("gps", "A", SAMPLE) == "accepted"  # holder unaffected

    _run(go())
    assert len(fixes) == 2                          # B's sample never published
    assert hub.feeder("gps") == "A"


def test_disconnect_frees_slot_for_automatic_takeover():
    hub, dev, fixes = _gps_setup()

    async def go():
        await dev.start()
        assert await hub.ingest("gps", "A", SAMPLE) == "accepted"
        hub.on_disconnect("A")                       # the ONLY automatic handover path
        assert await hub.ingest("gps", "B", SAMPLE) == "accepted"

    _run(go())
    assert hub.feeder("gps") == "B"


def test_helm_changes_never_touch_the_feeder():
    """Taking the helm is a control-role change; it must not reassign sensors.
    There is deliberately NO hub API tied to helm state — this pins that the
    holder survives anything short of its own disconnect."""
    hub, dev, fixes = _gps_setup()

    async def go():
        await dev.start()
        assert await hub.ingest("gps", "A", SAMPLE) == "accepted"
        # simulate 'B takes the helm' -> nothing on the hub changes
        assert hub.feeder("gps") == "A"
        assert await hub.ingest("gps", "B", SAMPLE) == "rejected"

    _run(go())
    assert hub.feeder("gps") == "A"


def test_inactive_without_a_built_device():
    hub = PhoneSensorHub()
    assert _run(hub.ingest("gps", "A", SAMPLE)) == "inactive"


def test_compass_publishes_magnetic_hdm():
    bus = EventBus()
    lines: list = []

    async def grab(s):
        lines.append(s)

    bus.subscribe(events.NMEA_IN, grab)
    hub = PhoneSensorHub()
    dev = PhoneCompass(hub, bus)

    async def go():
        await dev.start()
        assert await hub.ingest("compass", "A", {"heading": 372.5}) == "accepted"

    _run(go())
    assert len(lines) == 1 and "HDM" in lines[0] and "12.5" in lines[0]  # wrapped mod 360


def test_bad_samples_are_dropped_not_fatal():
    hub, dev, fixes = _gps_setup()

    async def go():
        await dev.start()
        assert await hub.ingest("gps", "A", {"lat": float("nan"), "lon": 1}) == "accepted"
        assert await hub.ingest("gps", "A", {}) == "accepted"

    _run(go())
    assert fixes == []                               # dropped, loop survived


def test_runtime_wiring_builds_phone_gps(tmp_path):
    from vanchor.app import Runtime
    from vanchor.core.config import load
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    cfg.hardware.gps_source = "phone"
    rt = Runtime(cfg)
    assert type(rt.gps).__name__ == "PhoneGps"
    assert "phone" in rt.device_config()["options"]["gps"]

    async def go():
        await rt.gps.start()      # device startup (sink registration) is async
        assert await rt.phone_ingest("gps", "X", SAMPLE) == "accepted"
        rt.phone_disconnect("X")
        assert await rt.phone_ingest("gps", "Y", SAMPLE) == "accepted"

    _run(go())
    assert "crude" in rt.device_debug("gps")["debug"]  # the disclaimer travels
