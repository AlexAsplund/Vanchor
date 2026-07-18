"""Hardware fingerprinting for the setup wizard (adoption pack #2).

SAFETY CONTRACT (do not weaken):
* Serial probing is PASSIVE by default: open, listen, close. Zero bytes
  are written to the candidate device.
* The sanctioned active serial writes are: (1) MOTOR_INFO_CMD (INFO+CRC,
  read-only identify, sent once per motor probe attempt); and (2) the
  8-byte UBX-MON-VER poll (read-only version query, sent only when the
  caller opts in AND the passive stage already classified the port as a
  GNSS candidate).  No CMD/STEERD/THRUST is ever written, so nothing can
  spin.
* I2C probing performs only register READS at explicitly named addresses
  (helm-Pico WHOAMI@0x42, INA226 MFR/DIE id) — never a bus-wide sweep,
  never a register write beyond the 1-byte read-pointer.

NOTE on motor identification (no HELLO/IDENT command exists):
There is NO HELLO or IDENT command in the vanchor motor protocol — verified
across firmware/ and src/vanchor/hardware/ (grep for HELLO finds nothing).
Adding one would violate the global constraint "No changes to the serial/i2c
protocol layers or motor controllers". Instead, motor identification is purely
PASSIVE: both firmware sketches broadcast CRC-8-suffixed feedback lines
unconditionally:

  * steering/steering.ino ~320-333: emits A <angle_deg> <ok> <wrap_pct> <seq>*HH
    every 100 ms (~10 Hz).
  * engine/engine.ino ~316-337: emits E <pwm> <dir> <state> <seq>*HH
    every 200 ms (~5 Hz), state in RUN|SOFTSTART|REVDELAY|FAILSAFE.

Opening the port at 115200 and listening for ~2 s positively identifies
vanchor firmware with zero writes. See task-4-brief.md §0.1 for full rationale.

The helm board firmware gained an INFO command (2026-07-18) for explicit
identification. The probe now sends MOTOR_INFO_CMD (INFO + CRC, read-only) and
parses the structured response as the PREFERRED fingerprint; old firmware that
does not answer INFO falls back to the passive A/E broadcast. CMD/STEERD/THRUST
are NEVER sent. The i2c-tunneled INFO variant is recorded as a TODO (no helm PCB
available for bench verification during this implementation session).

BENCH-VERIFY (INFO): firmware spec provided by owner 2026-07-18; not yet
verified on real hardware.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field


@dataclass
class ProbeResult:
    """Result of probing one serial port or I2C address."""

    detected: str = "unknown"   # see DETECTED values below
    confidence: str = "none"    # "high" | "medium" | "none"
    sample: dict = field(default_factory=dict)
    raw_preview: list[str] = field(default_factory=list)  # <= 8 lines / 4 hex rows
    counts: dict = field(default_factory=dict)  # evidence tallies


DETECTED = ("ublox", "nmea-gps", "nmea-compass", "nmea-depth",
            "witmotion-imu", "vanchor-motor", "unknown")

# UBX-MON-VER poll: build_frame(0x0A, 0x04, b"") — precomputed, asserted in tests.
UBX_MON_VER_POLL: bytes = b"\xb5\x62\x0a\x04\x00\x00\x0e\x34"

# Baud rate ladders for the wizard steps (duplicated in JS in hwwizard.js — keep in sync)
BAUD_LADDER = {
    "gps":     [38400, 9600, 4800, 115200],
    "compass": [9600, 115200, 4800],       # HWT901B factory default 9600
    "motor":   [115200, 4800],             # protocol v2 default; legacy 4800
    "any":     [115200, 38400, 9600, 4800],
}

# Motor feedback regexes (applied to the payload AFTER strip_verify_crc)
_A_RE = re.compile(r"^A -?\d+(\.\d+)? [01] -?\d+( \d+)?$")
_E_RE = re.compile(r"^E \d{1,3} [FR] (RUN|SOFTSTART|REVDELAY|FAILSAFE)( -?\d+)?$")

_GPS_TYPES = frozenset(("RMC", "GGA", "GLL", "VTG", "GSV", "GSA"))
_COMPASS_TYPES = frozenset(("HDT", "HDM", "HDG"))
_DEPTH_TYPES = frozenset(("DBT", "DPT"))


def _crc8(s: str) -> int:
    """CRC-8 (poly 0x07, init 0x00) for a string literal — pure stdlib."""
    crc = 0
    for ch in s.encode("ascii", errors="replace"):
        crc ^= ch
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


# Motor INFO request: CRC-appended "INFO" command + CRLF (read-only identify,
# no actuation). BENCH-VERIFY: requires helm board firmware >= 2026-07-18.
# CRC-8 of "INFO" precomputed here; asserted in tests via append_crc("INFO").
MOTOR_INFO_CMD: bytes = f"INFO*{_crc8('INFO'):02X}\r\n".encode()


# --------------------------------------------------------------------------- #
# Pure helpers (no I/O)
# --------------------------------------------------------------------------- #

def hint_from_metadata(path: str, description: str) -> str | None:
    """Metadata-only port hint derived from the path/description string.

    Never opens a port — pure string matching.
    """
    combined = (path + " " + description).lower()
    if "u-blox" in combined or "ublox" in combined:
        return "ublox"
    if "arduino" in combined or "ch340" in combined or "usb serial" in combined:
        return "maybe-motor"
    if "gps" in combined or "gnss" in combined:
        return "nmea-gps"
    return None


def suggest_for(detected: str, port: str, baud: int) -> dict | None:
    """Map a detected serial device type to the hardware config fields to save.

    Returns the full suggest dict (kind, source, fields) or None for unknown.
    NOTE: witmotion-imu uses hardware.baudrate, NOT compass_baud, because the
    hwt901b driver opens hw.compass_port at hw.baudrate (see hwt901b.py _build).
    """
    if detected == "ublox":
        return {"kind": "gps", "source": "ublox",
                "fields": {"gps_source": "ublox", "gps_port": port, "gps_baud": baud}}
    if detected == "nmea-gps":
        return {"kind": "gps", "source": "serial",
                "fields": {"gps_source": "serial", "gps_port": port, "gps_baud": baud}}
    if detected == "nmea-compass":
        return {"kind": "compass", "source": "serial",
                "fields": {"compass_source": "serial", "compass_port": port,
                           "compass_baud": baud}}
    if detected == "witmotion-imu":
        # IMPORTANT: hwt901b driver opens compass_port at hardware.baudrate (NOT compass_baud)
        return {"kind": "compass", "source": "hwt901b",
                "fields": {"compass_source": "hwt901b", "compass_port": port,
                           "baudrate": baud}}
    if detected == "vanchor-motor":
        return {"kind": "motor", "source": "serial",
                "fields": {"motor_source": "serial", "motor_port": port,
                           "motor_baud": baud}}
    return None


# --------------------------------------------------------------------------- #
# classify_bytes — deterministic, testable, no I/O
# --------------------------------------------------------------------------- #

def classify_bytes(data: bytes) -> ProbeResult:
    """Classify a raw byte buffer from a serial port into a known device type.

    Gathers all evidence first, then picks by priority (first hit wins).
    """
    from .serial_link import strip_verify_crc
    from ..nav import nmea as nmea_mod
    from ..nav import ubx as ubx_mod

    # --- 1. UBX binary frames ---
    frames, _ = ubx_mod.parse_stream(data)
    ubx_count = len(frames)
    ubx_sample: dict = {}
    for msg_class, msg_id, payload in frames:
        if ubx_mod.is_nav_pvt(msg_class, msg_id):
            pvt = ubx_mod.decode_nav_pvt(payload)
            if pvt is not None:
                ubx_sample = {
                    "lat": pvt.lat, "lon": pvt.lon,
                    "num_sv": pvt.num_sv, "fix_type": pvt.fix_type,
                    "valid": pvt.valid, "sog_knots": pvt.sog_knots,
                }

    # --- 2. WitMotion 11-byte frames: 0x55 TYPE d0..d7 SUM ---
    wit_count = 0
    wit_sample: dict = {}
    i = 0
    while i + 10 < len(data):
        b = data[i]
        if b == 0x55 and 0x50 <= data[i + 1] <= 0x5A:
            if sum(data[i:i + 10]) & 0xFF == data[i + 10]:
                wit_count += 1
                if data[i + 1] == 0x53:  # angle frame
                    d = data[i:i + 11]
                    roll = int.from_bytes(d[2:4], "little", signed=True) / 32768.0 * 180.0
                    pitch = int.from_bytes(d[4:6], "little", signed=True) / 32768.0 * 180.0
                    yaw = int.from_bytes(d[6:8], "little", signed=True) / 32768.0 * 180.0
                    wit_sample = {"roll_deg": roll, "pitch_deg": pitch, "yaw_deg": yaw}
                i += 11
                continue
        i += 1

    # --- 3. Motor A/E feedback lines ---
    motor_crc_ok = 0
    motor_nocrc = 0
    motor_sample: dict = {}
    decoded_lines: list[str] = data.decode("ascii", errors="replace").splitlines()
    for raw_line in decoded_lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload_str, verdict = strip_verify_crc(line)
        except Exception:
            continue
        if verdict is False:
            # CRC present but wrong — reject the line
            continue
        a_match = _A_RE.match(payload_str)
        e_match = _E_RE.match(payload_str)
        if a_match:
            if verdict is True:
                motor_crc_ok += 1
            elif verdict is None:
                motor_nocrc += 1
            parts = payload_str.split()
            try:
                motor_sample.update({
                    "angle_deg": float(parts[1]),
                    "feedback_ok": bool(int(parts[2])),
                    "wrap_pct": int(parts[3]),
                })
            except (IndexError, ValueError):
                pass
        if e_match:
            if verdict is True:
                motor_crc_ok += 1
            elif verdict is None:
                motor_nocrc += 1
            parts = payload_str.split()
            try:
                motor_sample.update({
                    "pwm": int(parts[1]),
                    "dir": parts[2],
                    "state": parts[3],
                })
            except (IndexError, ValueError):
                pass

    # --- 4. NMEA lines ---
    nmea_count = 0
    nmea_types_seen: dict[str, int] = {}
    nmea_sample: dict = {}
    for raw_line in decoded_lines:
        line = raw_line.strip()
        if not line.startswith("$"):
            continue
        if not nmea_mod.has_valid_checksum(line):
            continue
        nmea_count += 1
        msg_type = line[3:6]   # 3-char type, skipping talker id (2 chars after $)
        nmea_types_seen[msg_type] = nmea_types_seen.get(msg_type, 0) + 1
        # Extract sample from last RMC or GGA
        if msg_type in ("RMC", "GGA") and not nmea_sample:
            try:
                parsed = nmea_mod.parse(line)
                if parsed is not None:
                    if msg_type == "RMC":
                        nmea_sample = {"lat": parsed.point.lat, "lon": parsed.point.lon,
                                       "valid": parsed.valid}
                    elif msg_type == "GGA":
                        nmea_sample = {"lat": parsed.point.lat, "lon": parsed.point.lon,
                                       "valid": parsed.fix_quality > 0,
                                       "sats": parsed.satellites}
            except Exception:
                pass

    # --- Build raw_preview ---
    text_ev = nmea_count + motor_crc_ok + motor_nocrc
    bin_ev = ubx_count + wit_count
    raw_preview: list[str] = []
    if text_ev > 0 or bin_ev == 0:
        # ASCII-dominant: up to 8 sanitized lines
        count = 0
        for raw_line in decoded_lines:
            if count >= 8:
                break
            sanitized = "".join(c if c.isprintable() else "?" for c in raw_line.strip())[:90]
            if sanitized:
                raw_preview.append(sanitized)
                count += 1
    else:
        # Binary-dominant: up to 4 rows of 16 bytes each from the first 64 bytes
        for row_start in range(0, min(64, len(data)), 16):
            raw_preview.append(data[row_start:row_start + 16].hex(" "))
            if len(raw_preview) >= 4:
                break

    counts = {
        "nmea": nmea_count, "ubx": ubx_count,
        "motor": motor_crc_ok, "witmotion": wit_count,
    }

    # --- Priority classification (first hit wins) ---
    if ubx_count >= 1:
        confidence = "high" if ubx_count >= 2 else "medium"
        return ProbeResult(detected="ublox", confidence=confidence,
                           sample=ubx_sample, raw_preview=raw_preview, counts=counts)

    if motor_crc_ok >= 2:
        return ProbeResult(detected="vanchor-motor", confidence="high",
                           sample=motor_sample, raw_preview=raw_preview, counts=counts)
    if motor_nocrc >= 3:
        return ProbeResult(detected="vanchor-motor", confidence="medium",
                           sample=motor_sample, raw_preview=raw_preview, counts=counts)

    if wit_count >= 3:
        return ProbeResult(detected="witmotion-imu", confidence="high",
                           sample=wit_sample, raw_preview=raw_preview, counts=counts)

    if nmea_count >= 2:
        gps_count = sum(v for k, v in nmea_types_seen.items() if k in _GPS_TYPES)
        compass_count = sum(v for k, v in nmea_types_seen.items() if k in _COMPASS_TYPES)
        depth_count = sum(v for k, v in nmea_types_seen.items() if k in _DEPTH_TYPES)
        if gps_count > 0:
            return ProbeResult(detected="nmea-gps", confidence="high",
                               sample=nmea_sample, raw_preview=raw_preview, counts=counts)
        if compass_count > gps_count:  # compass_count > 0 (since gps_count == 0 here)
            return ProbeResult(detected="nmea-compass", confidence="high",
                               sample=nmea_sample, raw_preview=raw_preview, counts=counts)
        if depth_count > 0:
            return ProbeResult(detected="nmea-depth", confidence="high",
                               sample=nmea_sample, raw_preview=raw_preview, counts=counts)

    if nmea_count == 1:
        # Single line — best-guess from the type seen
        detected = "nmea-gps"  # default
        for msg_type in nmea_types_seen:
            if msg_type in _COMPASS_TYPES:
                detected = "nmea-compass"
                break
            if msg_type in _DEPTH_TYPES:
                detected = "nmea-depth"
                break
        return ProbeResult(detected=detected, confidence="medium",
                           sample=nmea_sample, raw_preview=raw_preview, counts=counts)

    return ProbeResult(detected="unknown", confidence="none",
                       raw_preview=raw_preview, counts=counts)


# --------------------------------------------------------------------------- #
# probe_serial — async, passive listener
# --------------------------------------------------------------------------- #

async def probe_serial(transport, duration_s: float = 2.0, *,
                       sleep=asyncio.sleep, max_bytes: int = 65536) -> ProbeResult:
    """Passively read from an already-open transport and classify what's on it.

    SAFETY: NEVER writes to the transport. Zero bytes sent. The caller is
    responsible for transport.open() and transport.close().

    Args:
        transport: An open SerialTransport supporting binary read(n).
        duration_s: Maximum listening time.
        sleep: Injected sleep coroutine (default asyncio.sleep; unused in body
               but kept for signature compatibility with callers that inject it).
        max_bytes: Stop after collecting this many bytes.

    Returns:
        ProbeResult with the best classification found within the time window.
    """
    start = time.monotonic()
    data = bytearray()
    last_classify = 0.0  # effectively "classify immediately after first read"

    while True:
        elapsed = time.monotonic() - start
        remaining = duration_s - elapsed
        if remaining <= 0:
            break
        if len(data) >= max_bytes:
            break

        try:
            chunk = await asyncio.wait_for(transport.read(4096), remaining)
            data.extend(chunk)
        except asyncio.TimeoutError:
            break
        except EOFError:
            break

        # Early exit: every ~0.25 s classify; stop on "high" confidence
        now = time.monotonic()
        if now - last_classify >= 0.25:
            last_classify = now
            result = classify_bytes(bytes(data))
            if result.confidence == "high":
                return result

    return classify_bytes(bytes(data))


# --------------------------------------------------------------------------- #
# ubx_mon_ver — optional active u-blox ident (8-byte write only)
# --------------------------------------------------------------------------- #

async def ubx_mon_ver(transport, timeout_s: float = 1.5) -> dict | None:
    """Poll UBX-MON-VER on an already-open transport and return the parsed result.

    SAFETY: Writes exactly 8 bytes (UBX_MON_VER_POLL = UBX-MON-VER poll request,
    a version READ query that changes NO receiver state). This is the same class
    of benign config query the existing UbloxGps driver performs.

    Args:
        transport: An open SerialTransport supporting read() and write().
        timeout_s: Maximum wait for a MON-VER response frame.

    Returns:
        {"sw": str, "hw": str, "extensions": [str, ...]} or None on timeout.
    """
    from ..nav import ubx as ubx_mod

    await transport.write(UBX_MON_VER_POLL)

    start = time.monotonic()
    data = bytearray()
    while True:
        remaining = timeout_s - (time.monotonic() - start)
        if remaining <= 0:
            break
        try:
            chunk = await asyncio.wait_for(transport.read(4096), remaining)
            data.extend(chunk)
        except (asyncio.TimeoutError, EOFError):
            break

        frames, _ = ubx_mod.parse_stream(bytes(data))
        for msg_class, msg_id, payload in frames:
            if msg_class == 0x0A and msg_id == 0x04:
                # Layout: 30 bytes swVersion (NUL-padded ASCII), 10 bytes hwVersion,
                # then 30-byte extension strings
                if len(payload) < 40:
                    return None
                sw = payload[:30].rstrip(b"\x00").decode("ascii", errors="replace")
                hw = payload[30:40].rstrip(b"\x00").decode("ascii", errors="replace")
                extensions: list[str] = []
                pos = 40
                while pos + 30 <= len(payload):
                    ext = payload[pos:pos + 30].rstrip(b"\x00").decode(
                        "ascii", errors="replace"
                    )
                    if ext:
                        extensions.append(ext)
                    pos += 30
                return {"sw": sw, "hw": hw, "extensions": extensions}

    return None


# --------------------------------------------------------------------------- #
# motor_info_probe — preferred motor identify via INFO command
# --------------------------------------------------------------------------- #

def parse_motor_info(lines: list[str]) -> dict | None:
    """Parse INFO response lines from the helm board firmware.

    Args:
        lines: Text lines from the serial port (may have CRC suffixes that
               are stripped tolerantly; non-INFO lines are ignored).

    Returns:
        dict with all parsed key-value pairs (unknown keys preserved for
        forward-compat), or None if no "I "-prefixed lines are found.

    INFO response format (each line may optionally carry a CRC-8 suffix)::

        I fw v1.2-3-gabc123 board helm-4.2 mcu pico2
        I proto 2.1 crc 1 wdog 800
        I conf 1 keys 23 flash stored
        I i2c 0x42 v1 active 0
        I up 7423 vbat 12.6 ang -3.2 fb 1
        I end 5
    """
    from .serial_link import strip_verify_crc

    info_lines: list[str] = []
    end_count: int | None = None

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        # Strip CRC if present (tolerant: ignore verdict)
        try:
            payload, _ = strip_verify_crc(raw)
        except Exception:
            payload = raw

        if payload.startswith("I end"):
            # Parse the count if present
            parts = payload.split()
            try:
                end_count = int(parts[2])
            except (IndexError, ValueError):
                end_count = len(info_lines)
            break
        if payload.startswith("I "):
            info_lines.append(payload)

    if not info_lines:
        return None

    result: dict = {}
    for iline in info_lines:
        tokens = iline[2:].split()  # strip leading "I "
        i = 0
        while i + 1 < len(tokens):
            result[tokens[i]] = tokens[i + 1]
            i += 2

    return result if result else None


async def motor_info_probe(transport, timeout_s: float = 2.0) -> dict | None:
    """Send INFO to a motor firmware and parse the structured response.

    SAFETY: Writes exactly MOTOR_INFO_CMD (INFO + CRC-8, read-only identify
    command; no motor state change; no CMD/STEERD/THRUST ever sent).
    Falls back gracefully to None if the firmware does not answer INFO
    (old firmware without the INFO command — the caller uses A/E passive
    fingerprint instead).

    BENCH-VERIFY: INFO command was added to helm board firmware 2026-07-18.
    Not yet verified on real hardware. The A/E fallback path is verified.

    Args:
        transport: An open SerialTransport supporting write() and read().
        timeout_s: Maximum wait for "I end" terminator.

    Returns:
        Parsed INFO dict (all key-value pairs from INFO lines), or None on
        timeout/no-response.
    """
    await transport.write(MOTOR_INFO_CMD)

    start = time.monotonic()
    data = bytearray()
    while True:
        remaining = timeout_s - (time.monotonic() - start)
        if remaining <= 0:
            break
        try:
            chunk = await asyncio.wait_for(transport.read(4096), remaining)
            data.extend(chunk)
        except (asyncio.TimeoutError, EOFError):
            break

        # Check for "I end" terminator
        decoded = data.decode("ascii", errors="replace")
        lines = decoded.splitlines()
        if any(l.strip().startswith("I end") or
               (l.strip().startswith("I end*") if "*" in l else False)
               for l in lines):
            return parse_motor_info(lines)

    # No terminator — try to parse whatever we have
    decoded = data.decode("ascii", errors="replace")
    lines = decoded.splitlines()
    return parse_motor_info(lines)


# --------------------------------------------------------------------------- #
# probe_i2c — synchronous I2C fingerprinting (runs in asyncio.to_thread)
# --------------------------------------------------------------------------- #

def probe_i2c(bus_num: int, addr: int, kind: str = "auto", *,
              smbus_factory=None) -> dict:
    """Probe one I2C address for known devices.

    SAFETY: Performs ONLY register reads at explicitly named addresses.
    Never a bus-wide address sweep. The only "write" is the 1-byte register
    pointer required by the SMBus protocol before reading.

    §4.4.4 helm-Pico (kind="helm-pico"): one combined i2c_rdwr(write([0x00]), read(2))
    at addr — reads WHOAMI (expect 0x56) and VERSION via auto-increment.
    §4.4.5 INA226 (kind="ina226"): two i2c_rdwr pairs at regs 0xFE (MFR_ID)
    and 0xFF (DIE_ID) — no data register write.
    §4.4.6 auto: try helm-pico first; on OSError (NAK) or WHOAMI mismatch,
    try ina226; report first success else detected="unknown".

    Args:
        bus_num: Linux I2C bus number (N in /dev/i2c-N).
        addr: 7-bit I2C slave address (0x03..0x77).
        kind: "helm-pico" | "ina226" | "auto".
        smbus_factory: Optional test seam: ``factory(bus_num) -> (bus, write_fn, read_fn)``.
    """
    if smbus_factory is None:
        try:
            from smbus2 import SMBus, i2c_msg  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "I2C probe requires the 'i2c' extra (pip install vanchor[i2c])"
            ) from exc
        _addr = addr

        def _factory(n: int):
            bus = SMBus(n)
            return (
                bus,
                lambda data: i2c_msg.write(_addr, list(data)),
                lambda n: i2c_msg.read(_addr, n),
            )

        smbus_factory = _factory

    bus, write_fn, read_fn = smbus_factory(bus_num)
    try:
        if kind in ("helm-pico", "auto"):
            try:
                return _probe_helm_pico(bus, addr, write_fn, read_fn)
            except OSError:
                if kind == "helm-pico":
                    return {"ok": True, "detected": "unknown"}
                # kind == "auto": fall through to ina226

        if kind in ("ina226", "auto"):
            try:
                return _probe_ina226(bus, addr, write_fn, read_fn)
            except OSError:
                pass

        return {"ok": True, "detected": "unknown"}
    finally:
        try:
            bus.close()
        except Exception:
            pass


def _probe_helm_pico(bus, addr: int, write_fn, read_fn) -> dict:
    """Probe for the helm-Pico at addr (§4.4.4). Raises OSError on failure."""
    w = write_fn(bytes([0x00]))  # pointer to WHOAMI register
    r = read_fn(2)
    bus.i2c_rdwr(w, r)
    data = bytes(r)
    whoami = data[0]
    version = data[1]
    if whoami != 0x56:
        raise OSError(f"helm-Pico WHOAMI=0x{whoami:02X} (expected 0x56) at addr=0x{addr:02X}")
    return {
        "ok": True,
        "detected": "helm-pico",
        "sample": {"whoami": "0x56", "version": version, "version_ok": version == 0x01},
    }


def _probe_ina226(bus, addr: int, write_fn, read_fn) -> dict:
    """Probe for the INA226 at addr (§4.4.5). Raises OSError on failure."""
    # MFR_ID @ 0xFE — big-endian "TI" = 0x5449
    w = write_fn(bytes([0xFE]))
    r = read_fn(2)
    bus.i2c_rdwr(w, r)
    mfr_raw = bytes(r)
    mfr_id = (mfr_raw[0] << 8) | mfr_raw[1]
    if mfr_id != 0x5449:
        raise OSError(f"INA226 MFR_ID=0x{mfr_id:04X} (expected 0x5449) at addr=0x{addr:02X}")
    # DIE_ID @ 0xFF — big-endian 0x2260
    w = write_fn(bytes([0xFF]))
    r = read_fn(2)
    bus.i2c_rdwr(w, r)
    die_raw = bytes(r)
    die_id = (die_raw[0] << 8) | die_raw[1]
    if die_id != 0x2260:
        raise OSError(f"INA226 DIE_ID=0x{die_id:04X} (expected 0x2260) at addr=0x{addr:02X}")
    return {
        "ok": True,
        "detected": "ina226",
        "sample": {"mfr_id": "0x5449", "die_id": "0x2260"},
    }
