/*
 * hotrc_translator.ino  --  HotRC receiver → Vanchor rf-remote protocol bridge.
 *
 * Reads PWM pulse widths from a standard RC receiver (HotRC or any PPM/PWM
 * receiver) and emits the Vanchor rf-remote text protocol over USB serial to the
 * Raspberry Pi. The Pi's rf-remote connector then bridges these commands into the
 * governed motor path — no direct motor wiring from this board.
 *
 * Channel mapping (configurable below):
 *   CH1 (PIN_CH_STEER)  → steering  [-1.0 .. +1.0]
 *   CH2 (PIN_CH_THRUST) → thrust    [-1.0 .. +1.0]
 *   CH3 (PIN_CH_MODE)   → mode button: ANCHOR / MANUAL / STOP
 *
 * Protocol output (to Pi, USB serial @ 115200 baud):
 *   "STICK <thrust> <steer>\r\n"   — every UPDATE_INTERVAL_MS while sticks move
 *   "BTN ANCHOR\r\n"               — when CH3 triggers anchor position
 *   "BTN MANUAL\r\n"               — when CH3 triggers manual mode
 *   "BTN STOP\r\n"                 — when CH3 triggers stop
 *   "PING\r\n"                     — periodic keep-alive when sticks are centred
 *
 * The rf-remote connector on the Pi handles the deadman switch (1 s timeout),
 * control grants, and safety. This board is a dumb translator — no motor logic.
 *
 * Hardware:
 *   - Arduino Nano / Uno (ATmega328P, 5V)
 *   - HotRC receiver (or any RC receiver with PWM outputs)
 *   - 3 signal wires from receiver channels to Arduino digital pins
 *   - Powered from Pi USB (5V) or receiver BEC
 *
 * Notes:
 *   - RC PWM is 1000-2000 µs (centre 1500). We normalise to [-1, 1].
 *   - A "no signal" condition (pulse < 800 or > 2200 µs) sends BTN STOP.
 *   - CH3 is typically a 2- or 3-position switch:
 *       Low  (~1000 µs) = STOP
 *       Mid  (~1500 µs) = MANUAL (sticks active)
 *       High (~2000 µs) = ANCHOR (hold current position)
 *   - Stick deadzone prevents jitter near centre from spamming commands.
 *
 * Target: Arduino Nano / Uno (ATmega328P). No external libraries required.
 */

// ─────────────────────────────── pin map ─────────────────────────────────── //
// Connect receiver CH1/CH2/CH3 signal wires to these pins. GND to common GND.
// These pins support Pin Change Interrupts on the ATmega328P.
const uint8_t PIN_CH_THRUST = 2;   // receiver CH2 → thrust (interrupt-capable)
const uint8_t PIN_CH_STEER  = 3;   // receiver CH1 → steering (interrupt-capable)
const uint8_t PIN_CH_MODE   = 4;   // receiver CH3 → mode switch

// ────────────────────────────── calibration ──────────────────────────────── //
// Standard RC PWM range. Adjust if your transmitter trims differently.
const int PWM_MIN       = 1000;  // µs — full negative (reverse / port)
const int PWM_CENTRE    = 1500;  // µs — centre (neutral)
const int PWM_MAX       = 2000;  // µs — full positive (forward / starboard)
const int PWM_DEADZONE  = 40;    // µs — ignore movement within ±deadzone of centre

// Signal validity window. Outside this = receiver lost / no signal.
const int PWM_VALID_MIN = 800;
const int PWM_VALID_MAX = 2200;

// CH3 mode switch thresholds (for a 3-position switch):
//   < MODE_STOP_THRESH      → STOP
//   STOP_THRESH .. ANCHOR_THRESH  → MANUAL
//   > MODE_ANCHOR_THRESH    → ANCHOR
const int MODE_STOP_THRESH   = 1250;  // µs — below this = STOP
const int MODE_ANCHOR_THRESH = 1750;  // µs — above this = ANCHOR

// ──────────────────────────────── timing ─────────────────────────────────── //
const unsigned long UPDATE_INTERVAL_MS = 50;   // stick update rate (~20 Hz)
const unsigned long PING_INTERVAL_MS   = 500;  // keep-alive when sticks idle
const unsigned long DEBOUNCE_MS        = 200;  // mode switch debounce

// ──────────────────────────────── state ──────────────────────────────────── //
// Volatile because updated in ISRs.
volatile unsigned long g_ch_thrust_rise = 0;
volatile unsigned long g_ch_steer_rise  = 0;
volatile int g_pw_thrust = PWM_CENTRE;  // last measured pulse width (µs)
volatile int g_pw_steer  = PWM_CENTRE;

unsigned long g_lastUpdateMs = 0;
unsigned long g_lastPingMs   = 0;
unsigned long g_lastModeChangeMs = 0;

// Mode tracking — only send BTN when the mode CHANGES.
enum Mode { MODE_STOP, MODE_MANUAL, MODE_ANCHOR };
Mode g_currentMode = MODE_MANUAL;
Mode g_lastSentMode = MODE_MANUAL;

bool g_signalLost = false;

// ─────────────────────── interrupt service routines ──────────────────────── //
// Pin 2 (INT0) — thrust channel
void isr_thrust() {
  if (digitalRead(PIN_CH_THRUST) == HIGH) {
    g_ch_thrust_rise = micros();
  } else {
    unsigned long pw = micros() - g_ch_thrust_rise;
    if (pw >= PWM_VALID_MIN && pw <= PWM_VALID_MAX) {
      g_pw_thrust = (int)pw;
    }
  }
}

// Pin 3 (INT1) — steering channel
void isr_steer() {
  if (digitalRead(PIN_CH_STEER) == HIGH) {
    g_ch_steer_rise = micros();
  } else {
    unsigned long pw = micros() - g_ch_steer_rise;
    if (pw >= PWM_VALID_MIN && pw <= PWM_VALID_MAX) {
      g_pw_steer = (int)pw;
    }
  }
}

// ────────────────────────── utility functions ─────────────────────────────── //

// Read CH3 (mode switch) via pulseIn — not interrupt-driven because mode
// changes are infrequent and we can afford the blocking read.
int readModePulse() {
  unsigned long pw = pulseIn(PIN_CH_MODE, HIGH, 25000);  // 25ms timeout
  if (pw == 0) return 0;  // timeout = no signal
  return (int)pw;
}

// Normalise a PWM pulse width to [-1.0, 1.0] with deadzone.
float normalise(int pw) {
  if (pw < PWM_VALID_MIN || pw > PWM_VALID_MAX) return 0.0f;

  int offset = pw - PWM_CENTRE;
  if (abs(offset) <= PWM_DEADZONE) return 0.0f;

  // Remove deadzone from the calculation range.
  float range = (float)(PWM_MAX - PWM_CENTRE - PWM_DEADZONE);
  float val;
  if (offset > 0) {
    val = (float)(offset - PWM_DEADZONE) / range;
  } else {
    val = (float)(offset + PWM_DEADZONE) / range;
  }

  // Clamp to [-1, 1].
  if (val > 1.0f) val = 1.0f;
  if (val < -1.0f) val = -1.0f;
  return val;
}

// Determine mode from CH3 pulse width.
Mode decodeMode(int pw) {
  if (pw == 0 || pw < PWM_VALID_MIN) return MODE_STOP;  // no signal = stop
  if (pw < MODE_STOP_THRESH)   return MODE_STOP;
  if (pw > MODE_ANCHOR_THRESH) return MODE_ANCHOR;
  return MODE_MANUAL;
}

// ──────────────────────────────── setup ──────────────────────────────────── //
void setup() {
  Serial.begin(115200);

  pinMode(PIN_CH_THRUST, INPUT);
  pinMode(PIN_CH_STEER, INPUT);
  pinMode(PIN_CH_MODE, INPUT);

  // Attach hardware interrupts for thrust and steering (pins 2, 3 on Uno/Nano).
  attachInterrupt(digitalPinToInterrupt(PIN_CH_THRUST), isr_thrust, CHANGE);
  attachInterrupt(digitalPinToInterrupt(PIN_CH_STEER), isr_steer, CHANGE);

  g_lastUpdateMs = millis();
  g_lastPingMs = millis();
}

// ──────────────────────────────── loop ───────────────────────────────────── //
void loop() {
  unsigned long now = millis();

  // ── Read mode switch (CH3) ──────────────────────────────────────────────
  int modePw = readModePulse();
  Mode newMode = decodeMode(modePw);

  // Signal loss detection: if CH3 reads 0 (timeout), signal is lost.
  if (modePw == 0) {
    if (!g_signalLost) {
      g_signalLost = true;
      Serial.print("BTN STOP\r\n");
    }
    delay(100);
    return;  // keep trying until signal returns
  }

  if (g_signalLost) {
    g_signalLost = false;
    // Signal recovered — send current mode on next iteration.
    g_lastSentMode = MODE_MANUAL;  // force re-send
  }

  // ── Mode change detection (debounced) ───────────────────────────────────
  if (newMode != g_currentMode && (now - g_lastModeChangeMs) > DEBOUNCE_MS) {
    g_currentMode = newMode;
    g_lastModeChangeMs = now;
  }

  // Send mode button only on CHANGE.
  if (g_currentMode != g_lastSentMode) {
    switch (g_currentMode) {
      case MODE_STOP:
        Serial.print("BTN STOP\r\n");
        break;
      case MODE_MANUAL:
        Serial.print("BTN MANUAL\r\n");
        break;
      case MODE_ANCHOR:
        Serial.print("BTN ANCHOR\r\n");
        break;
    }
    g_lastSentMode = g_currentMode;
  }

  // ── Stick updates (only in MANUAL mode) ─────────────────────────────────
  if (g_currentMode == MODE_MANUAL && (now - g_lastUpdateMs) >= UPDATE_INTERVAL_MS) {
    g_lastUpdateMs = now;

    // Read volatile values with interrupts briefly disabled for consistency.
    noInterrupts();
    int pwThrust = g_pw_thrust;
    int pwSteer  = g_pw_steer;
    interrupts();

    float thrust = normalise(pwThrust);
    float steer  = normalise(pwSteer);

    // Only send if sticks are NOT both centred (deadzone).
    if (thrust != 0.0f || steer != 0.0f) {
      char buf[40];
      // dtostrf for float-to-string on AVR (no printf %f).
      char tStr[8], sStr[8];
      dtostrf(thrust, 0, 2, tStr);
      dtostrf(steer, 0, 2, sStr);
      snprintf(buf, sizeof(buf), "STICK %s %s", tStr, sStr);
      Serial.print(buf);
      Serial.print("\r\n");
      g_lastPingMs = now;  // stick update counts as activity
    } else if ((now - g_lastPingMs) >= PING_INTERVAL_MS) {
      // Sticks centred for a while — send periodic keep-alive.
      Serial.print("PING\r\n");
      g_lastPingMs = now;
    }
  }

  // In ANCHOR or STOP mode, send periodic PING to show we're alive.
  if (g_currentMode != MODE_MANUAL && (now - g_lastPingMs) >= PING_INTERVAL_MS) {
    Serial.print("PING\r\n");
    g_lastPingMs = now;
  }
}
