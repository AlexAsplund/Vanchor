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
 *     CMD <pwm> <dir> <steer> [<seq>]\r\n
 *
 *   <pwm>    integer 0..255   magnitude of thrust  (0 = stop)
 *   <dir>    'F' or 'R'       drive direction (F = ahead, R = astern)
 *   <steer>  integer -100..100  steering: -100 hard port, +100 hard starboard,
 *                               0 = centred (dead ahead)
 *   <seq>    OPTIONAL integer >=0  heartbeat sequence number (roadmap #18). When
 *                               the Pi arms the heartbeat it appends a wrapping
 *                               seq to every CMD; the firmware echoes the last
 *                               seq it parsed back in its A/E feedback so the Pi
 *                               can detect a ONE-WAY serial failure. Absent when
 *                               the Pi has not armed the heartbeat -> parses as
 *                               seq = -1 (backward compatible with an older Pi).
 *
 *   The Pi already maps the normalized MotorCommand as:
 *       pwm   = round(|thrust| * 255)          thrust in [-1, 1]
 *       dir   = 'R' if thrust < 0 else 'F'
 *       steer = round(steering * 100)          steering in [-1, 1]
 *
 *   Since protocol v2 the Pi appends a "*HH" CRC-8 suffix (see below).
 *   Examples (with their real CRCs):
 *       CMD 0 F 0*DC          stopped, centred
 *       CMD 255 F 0*F0        full ahead, centred
 *       CMD 128 R -100*3B     half astern, hard port
 *
 *   NOTE: a single CMD line carries BOTH thrust and steering. The engine board
 *   uses pwm+dir and ignores steer; the steering board uses steer and ignores
 *   pwm+dir. Each board has its own USB-serial port (USB CDC is point-to-point,
 *   not broadcast; a USB hub does not fan the data out to all connected devices).
 *   The Pi opens one port per device and sends the same CMD line to both.
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
 *     A <angle_deg> <ok> <wrap_pct> [<seq>]\r\n
 *
 *   <angle_deg>  float, actual steering azimuth, signed deg (port -, stbd +)
 *   <ok>         '1' feedback healthy, '0' pot reading implausible / lost
 *   <wrap_pct>   int, -100..100, cable-wrap usage
 *   <seq>        int, heartbeat echo (roadmap #18): the seq of the last CMD this
 *                board parsed, or -1 if it has parsed none yet. Always emitted by
 *                this firmware; an older Pi simply ignores the extra field.
 *
 *   Example:  A -12.4 1 -7 42\r\n
 *
 *   The engine board similarly acknowledges its applied state (optional, handy
 *   for debugging / a future thrust-feedback channel):
 *
 *     E <pwm> <dir> <state> [<seq>]\r\n  state = RUN|SOFTSTART|REVDELAY|FAILSAFE
 *
 *   <seq> is the same heartbeat echo as the A line.
 *
 *   >>> Pi-side note (roadmap #18): SerialMotorController parses BOTH the 'A'
 *   steering feedback and the 'E' engine status off its read-loop, and — when
 *   the heartbeat is armed — tracks the echoed <seq> to detect a one-way serial
 *   failure via the existing per-device health flag. A firmware that does not
 *   echo seq is treated as "unknown", never failed, so it cannot brick the Pi.
 *
 * ----------------------------------------------------------------- HEARTBEAT
 * Loss-of-signal failsafe: if no valid CMD arrives within VANCHOR_WATCHDOG_MS,
 * each board ramps to a safe state (engine -> neutral/stop; steering -> hold,
 * optional slow centre). This mirrors the Pi safety governor's loss-of-fix
 * behaviour (controller/safety.py).
 */
#ifndef VANCHOR_PROTOCOL_H
#define VANCHOR_PROTOCOL_H

// Match the Pi's motor_baud/steering_baud/thrust_baud (core/config.py,
// default 115200 since protocol v2). Native-USB boards ignore the number;
// a real UART must agree on both ends. Change identically in both places.
#ifndef VANCHOR_BAUD
#define VANCHOR_BAUD 115200
#endif

// Protocol v2: every line MAY carry a trailing "*HH" — CRC-8 (poly 0x07,
// init 0x00, unreflected) over all characters before the '*', two uppercase
// hex digits. The Pi always sends it and verifies it on feedback.
//   Compatibility matrix (each side degrades safely):
//     new Pi  + old fw : old parser ignores the "*HH" tail -> drives fine
//     old Pi  + new fw : no CRC on CMD -> rejected when VANCHOR_REQUIRE_CRC,
//                        watchdog holds the safe state (visible, safe)
//     new Pi  + new fw : full integrity both directions
// Set VANCHOR_REQUIRE_CRC to 0 to accept CRC-less commands from an older Pi.
#ifndef VANCHOR_REQUIRE_CRC
#define VANCHOR_REQUIRE_CRC 1
#endif

// Failsafe: ramp to safe state if no valid CMD within this window (ms).
#ifndef VANCHOR_WATCHDOG_MS
#define VANCHOR_WATCHDOG_MS 800
#endif

// Max characters in one inbound line (CMD ... is short; be generous).
#define VANCHOR_LINE_MAX 48

// Upper bound for a parsed heartbeat seq (roadmap #18). The Pi wraps its seq
// modulo 10000, so any value at/above this is out-of-range garbage; clamp to
// keep the echo bounded and prevent long-integer overflow on a malformed tail.
#ifndef VANCHOR_SEQ_MAX
#define VANCHOR_SEQ_MAX 65535
#endif

/*
 * Parsed command line. Returns true on a well-formed
 * "CMD <pwm> <dir> <steer> [<seq>]".
 *  pwm   filled 0..255
 *  dir   filled 'F' or 'R'
 *  steer filled -100..100
 *  seq   OPTIONAL out (may be NULL). Filled with the trailing heartbeat seq
 *        (>=0, clamped to VANCHOR_SEQ_MAX) when present, or -1 when the CMD has
 *        no seq field (an older Pi). Only written when the whole line parses.
 * Tolerates extra spaces and a trailing '\r'. Leaves outputs untouched + returns
 * false on any malformed line so the caller keeps the last good command.
 *
 * Backward-compatible: the seq argument defaults to NULL, so existing callers
 * (and a CMD line without a seq field) are entirely unaffected.
 */
inline bool vanchorParseCmd(const char *line, int *pwm, char *dir, int *steer,
                            int *seq = 0) {
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

  // --- seq (OPTIONAL trailing non-negative integer) ---
  // Absent -> -1 (older Pi, heartbeat not armed). A present-but-non-numeric
  // tail (e.g. an old debugging suffix) is ignored, also yielding -1, so it can
  // never turn a valid command into a rejection.
  long q = -1;
  const char *pq = p;
  while (*pq == ' ') pq++;
  if (*pq >= '0' && *pq <= '9') {
    q = 0;
    while (*pq >= '0' && *pq <= '9') { q = q * 10 + (*pq - '0'); pq++; }
    if (q > VANCHOR_SEQ_MAX) q = VANCHOR_SEQ_MAX;
  }

  *pwm = (int)v;
  *dir = d;
  *steer = (int)s;
  if (seq) *seq = (int)q;
  return true;
}

/*
 * CRC-8 (poly 0x07, init 0x00, unreflected) over `len` chars — mirrors
 * crc8() in src/vanchor/hardware/serial_link.py exactly.
 */
inline unsigned char vanchorCrc8(const char *s, unsigned int len) {
  unsigned char crc = 0;
  for (unsigned int i = 0; i < len; i++) {
    crc ^= (unsigned char)s[i];
    for (unsigned char b = 0; b < 8; b++)
      crc = (crc & 0x80) ? (unsigned char)((crc << 1) ^ 0x07)
                         : (unsigned char)(crc << 1);
  }
  return crc;
}

/*
 * Verify-and-strip a trailing "*HH" CRC suffix IN PLACE.
 *   returns  1  suffix present and correct (line truncated at the '*')
 *            0  suffix present but WRONG (line truncated; caller must reject)
 *           -1  no suffix (older Pi; line untouched)
 * A '*' not followed by exactly two hex digits at end-of-line is not a suffix.
 */
inline int vanchorCheckCrc(char *line) {
  unsigned int len = 0;
  while (line[len]) len++;
  // tolerate a trailing '\r' the way the parser does
  unsigned int end = len;
  while (end > 0 && (line[end - 1] == '\r' || line[end - 1] == '\n')) end--;
  if (end < 3 || line[end - 3] != '*') return -1;
  unsigned char want = 0;
  for (unsigned int i = end - 2; i < end; i++) {
    char c = line[i];
    unsigned char v;
    if (c >= '0' && c <= '9') v = c - '0';
    else if (c >= 'A' && c <= 'F') v = c - 'A' + 10;
    else if (c >= 'a' && c <= 'f') v = c - 'a' + 10;
    else return -1;
    want = (unsigned char)((want << 4) | v);
  }
  line[end - 3] = '\0';                       // strip "*HH" (and the tail)
  return vanchorCrc8(line, end - 3) == want ? 1 : 0;
}

/*
 * Gate an inbound line per VANCHOR_REQUIRE_CRC. Strips a valid suffix in
 * place so the line is ready for vanchorParseCmd(). Call BEFORE parsing:
 *
 *     if (vanchorAcceptLine(g_line) && vanchorParseCmd(g_line, ...)) { ... }
 */
inline bool vanchorAcceptLine(char *line) {
  int v = vanchorCheckCrc(line);
  if (v == 1) return true;          // verified (and stripped)
  if (v == 0) return false;         // corrupted: never act on it
#if VANCHOR_REQUIRE_CRC
  return false;                     // no CRC and we require one (old Pi)
#else
  return true;                      // no CRC tolerated (VANCHOR_REQUIRE_CRC=0)
#endif
}

/*
 * Append "*HH" to a NUL-terminated outbound line (before the "\r\n").
 * No-op if the buffer lacks the 4 spare bytes.
 */
inline void vanchorAppendCrc(char *buf, unsigned int cap) {
  unsigned int len = 0;
  while (buf[len]) len++;
  if (len + 4 > cap) return;
  unsigned char c = vanchorCrc8(buf, len);
  const char *hex = "0123456789ABCDEF";
  buf[len] = '*';
  buf[len + 1] = hex[c >> 4];
  buf[len + 2] = hex[c & 0x0F];
  buf[len + 3] = '\0';
}

#endif  // VANCHOR_PROTOCOL_H
