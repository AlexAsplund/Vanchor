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


def test_reissue_bridges_quiet_spells_but_respects_the_cap():
    """Browsers coalesce timers, so the stream is bursty: the device re-publishes
    the last fix while quiet (age 1..15 s) and goes silent past the cap."""
    bus = EventBus()
    fixes: list = []

    async def grab(fix):
        fixes.append(fix)

    bus.subscribe(events.GPS_FIX_IN, grab)
    now = [100.0]
    hub = PhoneSensorHub(mono_fn=lambda: now[0])
    dev = PhoneGps(hub, bus, mono_fn=lambda: now[0])

    async def go():
        await dev.start()
        await hub.ingest("gps", "A", SAMPLE)          # 1 real fix
        await dev._reissue_if_quiet(now[0])           # age 0 -> no reissue
        assert len(fixes) == 1
        now[0] = 103.0
        await dev._reissue_if_quiet(now[0])           # quiet 3s -> reissue
        assert len(fixes) == 2
        assert fixes[1].point.lat == fixes[0].point.lat
        now[0] = 116.0
        await dev._reissue_if_quiet(now[0])           # 16s > cap -> SILENT
        assert len(fixes) == 2
        await dev.stop()

    _run(go())


def test_reissue_stays_silent_without_a_feeder():
    bus = EventBus()
    fixes: list = []

    async def grab(fix):
        fixes.append(fix)

    bus.subscribe(events.GPS_FIX_IN, grab)
    now = [100.0]
    hub = PhoneSensorHub(mono_fn=lambda: now[0])
    dev = PhoneGps(hub, bus, mono_fn=lambda: now[0])

    async def go():
        await dev.start()
        await hub.ingest("gps", "A", SAMPLE)
        hub.on_disconnect("A")                        # phone vanished
        now[0] = 103.0
        await dev._reissue_if_quiet(now[0])
        assert len(fixes) == 1                        # no ghost fixes
        await dev.stop()

    _run(go())


def test_sparse_phone_stream_no_longer_trips_fix_lost(tmp_path):
    """End-to-end: real fixes every 5.9s (the recorded field cadence) with NO
    client-side resends must not pulse the loss-of-fix failsafe, because the
    device's 1 Hz reissue loop keeps the navigator fed."""
    from vanchor.app import Runtime
    from vanchor.core.config import load
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    cfg.hardware.gps_source = "phone"
    rt = Runtime(cfg)

    async def go():
        await rt.gps.start()
        ctrl = rt.controller
        mono = rt.gps._mono   # real monotonic; drive reissue manually with fake ages
        episodes = []
        lost = False
        next_real = 0.0
        # simulate 60s at 5Hz; drive _reissue_if_quiet on a 1s grid like the task
        base = 1000.0
        rt.gps._mono = lambda: base + t[0]
        t = [0.0]
        for i in range(300):
            t[0] = round(i * 0.2, 3)
            if t[0] >= next_real:
                await rt.phone_ingest("gps", "X", SAMPLE)
                next_real += 5.9
            if abs(t[0] % 1.0) < 1e-9:
                await rt.gps._reissue_if_quiet(base + t[0])
            ctrl.control_tick(0.2)
            fl = ctrl.safety_status.fix_lost
            if fl and not lost:
                episodes.append(t[0]); lost = True
            if not fl and lost:
                lost = False
        assert episodes == [], f"fix_lost pulsed at {episodes}"
        await rt.gps.stop()

    _run(go())
