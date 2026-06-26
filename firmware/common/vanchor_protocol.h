/*
 * vanchor_protocol.h  --  shared line protocol for the Vanchor-NG hardware bridge.
 *
 * The autopilot runs on a Raspberry Pi and commands the motor + steering
 * hardware through an Arduino over USB serial. This header captures the EXACT
 * ASCII line protocol the Pi already speaks in
 *   src/vanchor/hardware/serial_devices.py  (SerialMotorController)
 *   src/vanchor/hardware/serial_link.py      (line framing + baud)
 * so the firmware is plug-compatible. Both engine.ino and steering.ino include
 * this file; each one ignores the fields it doesn't use.
 *
 * ------------------------------------------------------------------ FRAMING
 * Transport (serial_link.py):
 *   - 8N1 ASCII, newline-delimited.
 *   - The Pi WRITES lines terminated with "\r\n".
 *   - The Pi READS with readline() and strips a trailing "\r\n".
 *   => Firmware must accept lines ending in '\n' (tolerate a leading/trailing
 *      '\r'), and must terminate every line it sends with "\r\n".
 *
 * Baud: 4800 by default (HardwareConfig.baudrate in core/config.py). It is a
 * single config knob shared by GPS/compass/motor; keep VANCHOR_BAUD in sync
 * with whatever HardwareConfig.baudrate is set to on the Pi.
 *
 * ------------------------------------------------------ COMMAND (Pi -> Arduino)
 * SerialMotorController._format() emits, once per control tick:
 *
 *     CMD <pwm> <dir> <steer>\r\n
 *
 *   <pwm>    integer 0..255   magnitude of thrust  (0 = stop)
 *   <dir>    'F' or 'R'       drive direction (F = ahead, R = astern)
 *   <steer>  integer -100..100  steering: -100 hard port, +100 hard starboard,
 *                               0 = centred (dead ahead)
 *
 *   The Pi already maps the normalized MotorCommand as:
 *       pwm   = round(|thrust| * 255)          thrust in [-1, 1]
 *       dir   = 'R' if thrust < 0 else 'F'
 *       steer = round(steering * 100)          steering in [-1, 1]
 *
 *   Examples:
 *       CMD 0 F 0          stopped, centred
 *       CMD 255 F 0        full ahead, centred
 *       CMD 128 R -100     half astern, hard port
 *
 *   NOTE: a single CMD line carries BOTH thrust and steering. The engine board
 *   uses pwm+dir and ignores steer; the steering board uses steer and ignores
 *   pwm+dir. If both boards sit on one USB hub they each see and filter the same
 *   line. (You may also run two CMD streams on two ports -- the Pi opens a
 *   separate transport per device.)
 *
 * --------------------------------------------------- FEEDBACK (Arduino -> Pi)
 * The Pi's telemetry (app.py ~line 1095) publishes a closed-loop steering block:
 *     steering.target_deg   desired angle  (Pi-side, pre-slew)
 *     steering.angle_deg    ACTUAL head angle   <-- must come from hardware
 *     steering.feedback_ok  feedback sensor healthy
 *     steering.wrap_pct     cable-wrap usage (angle / steer_range_deg)
 *
 * To feed that, the steering board reports its measured angle on a feedback
 * line. The existing _SerialNmeaSensor read-loop publishes every inbound line
 * onto the bus verbatim, so we use an NMEA-style framing that is trivial to
 * parse and won't be confused with GPS/compass sentences:
 *
 *     A <angle_deg> <ok> <wrap_pct>\r\n
 *
 *   <angle_deg>  float, actual steering azimuth, signed deg (port -, stbd +)
 *   <ok>         '1' feedback healthy, '0' pot reading implausible / lost
 *   <wrap_pct>   int, -100..100, cable-wrap usage
 *
 *   Example:  A -12.4 1 -7\r\n
 *
 *   The engine board similarly acknowledges its applied state (optional, handy
 *   for debugging / a future thrust-feedback channel):
 *
 *     E <pwm> <dir> <state>\r\n      state = RUN | SOFTSTART | REVDELAY | FAILSAFE
 *
 *   >>> Pi-side note: SerialMotorController today only WRITES; to consume the 'A'
 *   feedback line, the Pi's motor transport read-loop should parse 'A ...' and
 *   set state.steering angle/feedback_ok. Until then the lines are harmless
 *   (ignored / logged). This is the only Pi-side change required and is
 *   documented in docs/firmware.md.
 *
 * ----------------------------------------------------------------- HEARTBEAT
 * Loss-of-signal failsafe: if no valid CMD arrives within VANCHOR_WATCHDOG_MS,
 * each board ramps to a safe state (engine -> neutral/stop; steering -> hold,
 * optional slow centre). This mirrors the Pi safety governor's loss-of-fix
 * behaviour (controller/safety.py).
 */
#ifndef VANCHOR_PROTOCOL_H
#define VANCHOR_PROTOCOL_H

// Match HardwareConfig.baudrate (core/config.py, default 4800). If you bump the
// Pi to 9600/57600/115200 for headroom, change it identically in both places.
#ifndef VANCHOR_BAUD
#define VANCHOR_BAUD 4800
#endif

// Failsafe: ramp to safe state if no valid CMD within this window (ms).
#ifndef VANCHOR_WATCHDOG_MS
#define VANCHOR_WATCHDOG_MS 800
#endif

// Max characters in one inbound line (CMD ... is short; be generous).
#define VANCHOR_LINE_MAX 48

/*
 * Parsed command line. Returns true on a well-formed "CMD <pwm> <dir> <steer>".
 *  pwm   filled 0..255
 *  dir   filled 'F' or 'R'
 *  steer filled -100..100
 * Tolerates extra spaces and a trailing '\r'. Leaves outputs untouched + returns
 * false on any malformed line so the caller keeps the last good command.
 */
inline bool vanchorParseCmd(const char *line, int *pwm, char *dir, int *steer) {
  // Skip leading spaces.
  while (*line == ' ') line++;
  if (line[0] != 'C' || line[1] != 'M' || line[2] != 'D') return false;
  const char *p = line + 3;

  // --- pwm ---
  while (*p == ' ') p++;
  if (*p < '0' || *p > '9') return false;
  long v = 0;
  while (*p >= '0' && *p <= '9') { v = v * 10 + (*p - '0'); p++; }
  if (v < 0) v = 0;
  if (v > 255) v = 255;

  // --- dir ---
  while (*p == ' ') p++;
  char d = *p;
  if (d != 'F' && d != 'R') return false;
  p++;

  // --- steer (optional leading '-') ---
  while (*p == ' ') p++;
  int sign = 1;
  if (*p == '-') { sign = -1; p++; }
  else if (*p == '+') { p++; }
  if (*p < '0' || *p > '9') return false;
  long s = 0;
  while (*p >= '0' && *p <= '9') { s = s * 10 + (*p - '0'); p++; }
  s *= sign;
  if (s < -100) s = -100;
  if (s > 100) s = 100;

  *pwm = (int)v;
  *dir = d;
  *steer = (int)s;
  return true;
}

#endif  // VANCHOR_PROTOCOL_H
