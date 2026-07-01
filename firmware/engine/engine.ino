/*
 * engine.ino  --  Vanchor-NG engine (thrust) board.
 *
 * Role: receive the autopilot's normalized thrust over USB serial and drive a
 * HIJACKED off-the-shelf DC / trolling-motor speed controller, plus a DPDT
 * relay for forward/reverse. We do NOT build a high-current driver here: we tap
 * into a commercial PWM "DC motor speed controller" (the cheap 6-60V boards with
 * a 10k speed knob, or a trolling-motor variable-speed control) and replace its
 * throttle knob with our signal. The big FETs, current sense and heatsink are
 * the bought controller's problem.
 *
 * Protocol: see ../common/vanchor_protocol.h. The Pi sends
 *     CMD <pwm> <dir> <steer>\r\n
 * This board uses <pwm> (0..255) and <dir> (F/R) and ignores <steer>.
 *
 * Safety (mirrors src/vanchor/controller/safety.py):
 *   - soft-start / slew limit on throttle (no slamming the prop),
 *   - reverse dead-time: throttle forced to 0 and held across a direction flip
 *     so the relay only switches with the motor unpowered (make-before-break /
 *     dead-time guard) and the bought ESC isn't shock-reversed,
 *   - serial watchdog: if commands stop, ramp to neutral (stop) and open the
 *     direction relay.
 *
 * ============================ HIJACK METHOD ============================
 * Two options are implemented; pick ONE at compile time via HIJACK_MODE.
 *
 *  (A) HIJACK_DIGIPOT  -- a digital potentiometer wired IN PLACE of the speed
 *      knob. Recommended when the controller's throttle is a 3-wire pot
 *      (top / wiper / bottom). An X9C103 (10k, up/down/cs) or MCP41010 (SPI)
 *      becomes the wiper the controller reads. Cleanest electrical match: the
 *      controller sees exactly what it expects, full 0..100% range, no analog
 *      offset/scaling guesswork. DEFAULT / RECOMMENDED.
 *
 *  (B) HIJACK_PWM_DAC  -- Arduino PWM pin -> RC low-pass filter -> 0..5V analog,
 *      (optionally op-amp buffered) fed into the controller's 0-5V throttle
 *      input. Use when the controller exposes a 0-5V analog throttle (many
 *      e-bike / hoverboard-style controllers) rather than a bare pot. Simpler
 *      wiring but you must match the controller's input impedance (buffer with a
 *      rail-to-rail op-amp like MCP6002 if the input loads the RC filter) and
 *      its full-scale voltage may be <5V (scale THROTTLE_MAX_PWM down).
 *
 * Trade-offs:
 *   DIGIPOT: exact range, monotonic, no analog drift, needs a pot-type input,
 *            3 extra wires (X9C: INC/UD/CS). Slight cost (~$1).
 *   PWM_DAC: works with any 0-5V analog input, 1 pin, but ripple/settling from
 *            the RC filter, needs a buffer if the input draws current, and you
 *            must trim full-scale. Choose when there's no pot to replace.
 *
 * NOTE the bought controller usually has its OWN reverse or it is forward-only.
 * If it has a reverse input, prefer that over our relay (set USE_DIR_RELAY 0 and
 * wire CTRL_REVERSE_PIN to it). Forward-only controllers use the DPDT relay
 * here to swap motor-output polarity -- ALWAYS at zero throttle (dead-time).
 */

#include "../common/vanchor_protocol.h"

// ----------------------------- configuration ------------------------------- //
#define HIJACK_DIGIPOT 1
#define HIJACK_PWM_DAC 2
#ifndef HIJACK_MODE
#define HIJACK_MODE HIJACK_DIGIPOT   // <-- recommended
#endif

// Use the on-board DPDT relay for F/R (forward-only controller). Set 0 if the
// bought controller has its own reverse input wired to CTRL_REVERSE_PIN.
#define USE_DIR_RELAY 1

// ------------------------------- pin map ----------------------------------- //
// (Arduino Nano / Uno, ATmega328P)
#if HIJACK_MODE == HIJACK_DIGIPOT
  // X9C103 digital pot (10k, 100 taps) standing in for the speed knob.
  const uint8_t PIN_DIGIPOT_INC = 5;  // /INC  : pulse to step the wiper
  const uint8_t PIN_DIGIPOT_UD  = 6;  // U/D   : HIGH = up (more throttle)
  const uint8_t PIN_DIGIPOT_CS  = 7;  // /CS   : LOW to select, HIGH latches
#else
  // PWM -> RC -> throttle. Use a 490Hz timer pin; D9 is fine on the 328P.
  const uint8_t PIN_THROTTLE_PWM = 9; // D9 -> 1k + 10uF RC -> (buffer) -> input
#endif

const uint8_t PIN_RELAY_DIR = 8;      // DPDT relay coil (via transistor): LOW=fwd, HIGH=rev
const uint8_t PIN_CTRL_REVERSE = A1;  // optional: drive controller's own reverse (if USE_DIR_RELAY 0)
const uint8_t PIN_STATUS_LED = 13;    // heartbeat / failsafe indicator

// ------------------------------ tuning ------------------------------------- //
// Soft-start / slew: max throttle change per second, expressed in 0..1 units.
// Matches SafetyConfig.max_thrust_slew_per_s (default 1.0 => full range in 1 s).
const float THROTTLE_SLEW_PER_S = 1.0f;

// Reverse dead-time: throttle must rest at ~0 for this long before a direction
// flip is allowed (matches SafetyConfig.reverse_delay_s, default 1.0 s, and the
// SerialMotorController reverse-delay). Guards the relay (make-before-break) and
// the ESC against shock reversal.
const unsigned long REVERSE_DEADTIME_MS = 1000;

// Below this normalized throttle the prop is treated as "stopped" for the
// purpose of the reverse interlock.
const float ZERO_THRUST_EPS = 0.02f;

#if HIJACK_MODE == HIJACK_PWM_DAC
// If the controller's analog throttle full-scale is < 5V, cap the PWM duty so we
// never exceed it. 255 = full 5V. (e.g. a 0-4.2V input -> ~214.)
const uint8_t THROTTLE_MAX_PWM = 255;
const uint8_t THROTTLE_MIN_PWM = 0;   // some controllers have a dead-zone; raise if needed
#endif

// ------------------------------ state -------------------------------------- //
float   g_targetThrottle = 0.0f;  // 0..1 magnitude requested (post reverse-gate)
float   g_appliedThrottle = 0.0f; // 0..1 magnitude currently output (slew-limited)
int8_t  g_targetDir = +1;         // +1 fwd, -1 rev (requested)
int8_t  g_appliedDir = +1;        // +1 fwd, -1 rev (relay actually set)
int8_t  g_lastNonZeroDir = +1;
unsigned long g_zeroSinceMs = 0;  // when throttle last fell to ~0 (for dead-time)
unsigned long g_lastCmdMs = 0;    // last valid CMD (for watchdog)
unsigned long g_lastTickMs = 0;

char    g_line[VANCHOR_LINE_MAX];
uint8_t g_lineLen = 0;

#if HIJACK_MODE == HIJACK_DIGIPOT
int     g_potTap = -1;            // 0..99 current X9C tap (-1 = unknown -> force min)
#endif

// ------------------------- digipot (X9C103) -------------------------------- //
#if HIJACK_MODE == HIJACK_DIGIPOT
// The X9C is stepped, not addressed: pulse INC with U/D set, count taps. We home
// to minimum at boot (step down 100+ times) so g_potTap is known absolutely.
//
// X9C103 NVM store rule (datasheet FN8222):
//   CS rising edge while INC is HIGH  → wiper position stored to NVM (EEPROM)
//   CS rising edge while INC is LOW   → no NVM store (wiper position unchanged in NVM)
// NVM endurance is ~100k cycles; every throttle adjustment would burn one cycle
// if we store on every CS deassertion. To avoid stores, INC must be LOW when CS
// rises. digipotPulse() therefore ends with INC LOW, not HIGH.
static void digipotPulse(bool up) {
  digitalWrite(PIN_DIGIPOT_UD, up ? HIGH : LOW);
  delayMicroseconds(3);
  // Bring INC HIGH first (idle level). If it was already HIGH this is a no-op
  // write; no wiper move on a LOW→HIGH edge. If it was LOW (e.g. from a prior
  // call), this restores it cleanly before the active falling edge.
  digitalWrite(PIN_DIGIPOT_INC, HIGH);
  delayMicroseconds(3);
  digitalWrite(PIN_DIGIPOT_INC, LOW);   // wiper moves on this HIGH→LOW falling edge
  delayMicroseconds(3);
  // Leave INC LOW so that the caller can raise CS without triggering an NVM store.
}

static void digipotHome() {
  digitalWrite(PIN_DIGIPOT_CS, LOW);
  for (int i = 0; i < 110; i++) digipotPulse(false);  // slam to minimum
  // After the last digipotPulse(), INC is LOW. CS rising with INC LOW → no NVM store.
  digitalWrite(PIN_DIGIPOT_CS, HIGH);
  g_potTap = 0;
}

// Move the wiper toward 'tap' (0..99). Only the steps needed are pulsed.
static void digipotSet(int tap) {
  if (tap < 0) tap = 0;
  if (tap > 99) tap = 99;
  if (g_potTap < 0) digipotHome();
  if (tap == g_potTap) return;
  digitalWrite(PIN_DIGIPOT_CS, LOW);
  while (g_potTap < tap) { digipotPulse(true);  g_potTap++; }
  while (g_potTap > tap) { digipotPulse(false); g_potTap--; }
  // After the last digipotPulse(), INC is LOW. CS rising with INC LOW → no NVM store.
  digitalWrite(PIN_DIGIPOT_CS, HIGH);
}
#endif

// --------------------------- throttle output ------------------------------- //
static void writeThrottle(float mag01) {
  if (mag01 < 0) mag01 = 0;
  if (mag01 > 1) mag01 = 1;
#if HIJACK_MODE == HIJACK_DIGIPOT
  digipotSet((int)(mag01 * 99.0f + 0.5f));
#else
  int duty = THROTTLE_MIN_PWM +
             (int)(mag01 * (THROTTLE_MAX_PWM - THROTTLE_MIN_PWM) + 0.5f);
  analogWrite(PIN_THROTTLE_PWM, duty);
#endif
}

// --------------------------- direction output ------------------------------ //
static void writeDirection(int8_t dir) {
#if USE_DIR_RELAY
  // Relay must only switch at zero throttle (caller guarantees the dead-time).
  digitalWrite(PIN_RELAY_DIR, dir < 0 ? HIGH : LOW);
#else
  digitalWrite(PIN_CTRL_REVERSE, dir < 0 ? HIGH : LOW);
#endif
  g_appliedDir = dir;
}

// ------------------------------- setup ------------------------------------- //
void setup() {
  Serial.begin(VANCHOR_BAUD);

  pinMode(PIN_RELAY_DIR, OUTPUT);
  pinMode(PIN_CTRL_REVERSE, OUTPUT);
  pinMode(PIN_STATUS_LED, OUTPUT);
  digitalWrite(PIN_RELAY_DIR, LOW);     // forward
  digitalWrite(PIN_CTRL_REVERSE, LOW);

#if HIJACK_MODE == HIJACK_DIGIPOT
  pinMode(PIN_DIGIPOT_INC, OUTPUT);
  pinMode(PIN_DIGIPOT_UD, OUTPUT);
  pinMode(PIN_DIGIPOT_CS, OUTPUT);
  // After pinMode(), Arduino output pins default LOW. Bring INC LOW before raising
  // CS so the initial CS LOW→HIGH transition doesn't trigger an NVM store.
  digitalWrite(PIN_DIGIPOT_INC, LOW);
  digitalWrite(PIN_DIGIPOT_CS, HIGH);   // deselect; INC is LOW → no NVM store
  digipotHome();                        // known throttle = 0 at boot
#else
  pinMode(PIN_THROTTLE_PWM, OUTPUT);
  analogWrite(PIN_THROTTLE_PWM, 0);
#endif

  writeThrottle(0.0f);
  writeDirection(+1);
  g_lastCmdMs = millis();
  g_lastTickMs = millis();
  g_zeroSinceMs = millis();
}

// --------------------------- command parsing ------------------------------- //
static void onCommand(int pwm, char dir, int /*steer ignored*/) {
  g_targetThrottle = (float)pwm / 255.0f;
  g_targetDir = (dir == 'R') ? -1 : +1;
  g_lastCmdMs = millis();
}

static void pollSerial() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (g_lineLen > 0) {
        g_line[g_lineLen] = '\0';
        int pwm, steer; char dir;
        if (vanchorParseCmd(g_line, &pwm, &dir, &steer)) onCommand(pwm, dir, steer);
      }
      g_lineLen = 0;
    } else if (g_lineLen < VANCHOR_LINE_MAX - 1) {
      g_line[g_lineLen++] = c;
    } else {
      g_lineLen = 0;  // overflow -> drop line
    }
  }
}

// ------------------------ control update (per tick) ------------------------ //
// Returns the magnitude (0..1) we are allowed to command after the reverse
// dead-time interlock, and updates g_appliedDir when a flip is permitted.
static float gateReverseAndDirection(float requestedMag, int8_t requestedDir, unsigned long now) {
  bool nearZero = (g_appliedThrottle <= ZERO_THRUST_EPS);
  if (nearZero) {
    if (g_zeroSinceMs == 0) g_zeroSinceMs = now;
  } else {
    g_zeroSinceMs = 0;
  }

  bool flip = (requestedDir != g_appliedDir);
  if (!flip) {
    if (requestedMag > ZERO_THRUST_EPS) g_lastNonZeroDir = requestedDir;
    return requestedMag;
  }

  // A direction change is requested. Force throttle to 0 and wait out the
  // dead-time at ~zero before switching the relay/controller-reverse.
  if (!nearZero) return 0.0f;                       // spin down first
  unsigned long rested = (g_zeroSinceMs == 0) ? 0 : (now - g_zeroSinceMs);
  if (rested < REVERSE_DEADTIME_MS) return 0.0f;    // hold at stop, keep waiting

  // Dead-time satisfied and throttle is ~0: switch direction now (relay makes
  // before break inherently because the motor is unpowered), then ramp up.
  writeDirection(requestedDir);
  g_lastNonZeroDir = requestedDir;
  return requestedMag;
}

static void updateControl() {
  unsigned long now = millis();
  float dt = (now - g_lastTickMs) / 1000.0f;
  if (dt <= 0) dt = 0.001f;
  g_lastTickMs = now;

  // --- watchdog failsafe: no valid CMD -> ramp to neutral, open direction --- //
  bool failsafe = (now - g_lastCmdMs) > VANCHOR_WATCHDOG_MS;
  float wantMag = failsafe ? 0.0f : g_targetThrottle;
  int8_t wantDir = failsafe ? g_appliedDir : g_targetDir;  // don't flip in failsafe; just stop

  // --- reverse dead-time + direction switching --- //
  float allowedMag = gateReverseAndDirection(wantMag, wantDir, now);

  // --- soft-start / slew limit on throttle magnitude --- //
  float maxStep = THROTTLE_SLEW_PER_S * dt;
  float delta = allowedMag - g_appliedThrottle;
  if (delta >  maxStep) delta =  maxStep;
  if (delta < -maxStep) delta = -maxStep;
  g_appliedThrottle += delta;
  if (g_appliedThrottle < 0) g_appliedThrottle = 0;
  if (g_appliedThrottle > 1) g_appliedThrottle = 1;

  writeThrottle(g_appliedThrottle);

  // --- status LED: solid in failsafe, heartbeat blink otherwise --- //
  if (failsafe) digitalWrite(PIN_STATUS_LED, HIGH);
  else digitalWrite(PIN_STATUS_LED, (now / 500) & 1);

  // --- optional applied-state ack for debugging (see protocol header) --- //
  static unsigned long lastReport = 0;
  if (now - lastReport >= 200) {
    lastReport = now;
    int pwmOut =
#if HIJACK_MODE == HIJACK_DIGIPOT
        (int)(g_appliedThrottle * 255.0f + 0.5f);
#else
        THROTTLE_MIN_PWM + (int)(g_appliedThrottle * (THROTTLE_MAX_PWM - THROTTLE_MIN_PWM) + 0.5f);
#endif
    const char *st = failsafe ? "FAILSAFE"
                   : (allowedMag < wantMag - 0.001f) ? "REVDELAY"
                   : (g_appliedThrottle + 0.001f < allowedMag) ? "SOFTSTART"
                   : "RUN";
    Serial.print("E ");
    Serial.print(pwmOut);
    Serial.print(g_appliedDir < 0 ? " R " : " F ");
    Serial.print(st);
    Serial.print("\r\n");
  }
}

// ------------------------------- loop -------------------------------------- //
void loop() {
  pollSerial();
  updateControl();
}
