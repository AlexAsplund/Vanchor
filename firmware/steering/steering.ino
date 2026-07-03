/*
 * steering.ino  --  Vanchor-NG closed-loop azimuth steering board.
 *
 * Role: hold the steering head (the rotating trolling-motor turret) at the
 * angle the autopilot commands, using a worm-gear DC gearmotor driven through an
 * H-bridge and a position-feedback POTENTIOMETER on the turret/ring as the angle
 * sensor. A PID position loop tracks the target; the worm gear self-locks so the
 * head holds with no holding current once settled.
 *
 *   (The CAD pack -- cad/steering_BOM.md -- prototypes this unit with an AS5600
 *   magnetic encoder over I2C. This firmware uses the pot variant the project
 *   selected; an AS5600 drop-in is documented in firmware/README.md: replace
 *   readAngleDeg() with an I2C read and you are done -- the loop is identical.)
 *
 * Protocol: see ../common/vanchor_protocol.h. The Pi sends
 *     CMD <pwm> <dir> <steer>\r\n
 * This board uses <steer> (-100..100, normalized) and ignores <pwm>/<dir>.
 *   target_deg = (steer/100) * STEER_RANGE_DEG
 * It reports actual angle back so the Pi's closed-loop telemetry
 * (steering.angle_deg / feedback_ok / wrap_pct, app.py) works:
 *     A <angle_deg> <ok> <wrap_pct>\r\n
 *
 * Mechanical params come from core/config.py BoatConfig:
 *     steer_range_deg     = 185.0   (+/- cable-wrap mechanical limit)
 *     max_steer_rate_dps  = 50.0    (head rotation speed; bounds PID output)
 *
 * Safety / robustness:
 *   - clamp target to +/- STEER_RANGE_DEG (cable-wrap soft endstops),
 *   - deadband around the target to stop hunting (worm self-locks in deadband),
 *   - output (PWM) limited; integrator clamped (anti-windup),
 *   - stall detection: large error + ~no motion for a while -> back off + flag,
 *   - feedback-plausibility check: pot reading out of the valid ADC window =>
 *     feedback_ok=0 and motor held (don't drive blind),
 *   - watchdog: no CMD within window -> HOLD current angle (worm locks); a slow
 *     optional recentre is provided but OFF by default (holding is safest).
 */

#include "../common/vanchor_protocol.h"

// ------------------------------- pin map ----------------------------------- //
// (Arduino Nano / Uno, ATmega328P) driving a BTS7960 H-bridge.
const uint8_t PIN_BTS_RPWM = 9;   // BTS7960 RPWM (drive one direction), 490Hz PWM
const uint8_t PIN_BTS_LPWM = 10;  // BTS7960 LPWM (drive the other direction)
const uint8_t PIN_BTS_R_EN = 4;   // R_EN enable (tie with L_EN; HIGH=run, LOW=coast)
const uint8_t PIN_BTS_L_EN = 7;   // L_EN enable
const uint8_t PIN_FEEDBACK_POT = A0;  // wiper of the steering feedback pot
const uint8_t PIN_BTS_R_IS = A2;   // optional BTS7960 current-sense (R_IS), analog
const uint8_t PIN_BTS_L_IS = A3;   // optional BTS7960 current-sense (L_IS), analog
const uint8_t PIN_STATUS_LED = 13;

// --------------------- mechanical / sensor calibration --------------------- //
// Cable-wrap mechanical limit, deg each side of centre (BoatConfig.steer_range_deg).
const float STEER_RANGE_DEG = 185.0f;
// Head rotation speed (BoatConfig.max_steer_rate_dps) -- used only as a sanity ref.
const float MAX_STEER_RATE_DPS = 50.0f;

// Feedback-pot calibration. A single-turn pot can only cover ~ +/-150deg of its
// ~300deg track; for +/-185deg use a multi-turn pot or gear the pot up. Measure
// the raw 10-bit ADC at the two mechanical extremes and at centre, then fill:
//   ADC_AT_NEG  : reading at -STEER_RANGE_DEG (hard port)
//   ADC_AT_POS  : reading at +STEER_RANGE_DEG (hard starboard)
// Angle is linearly interpolated. Keep a margin from 0 / 1023 so an open/short
// wiper reads outside [ADC_MIN_VALID, ADC_MAX_VALID] and trips feedback_ok=0.
const int ADC_AT_NEG = 60;     // <-- CALIBRATE
const int ADC_AT_POS = 963;    // <-- CALIBRATE
const int ADC_MIN_VALID = 8;   // below this => wiper shorted/open => feedback lost
const int ADC_MAX_VALID = 1015;// above this => wiper open => feedback lost

// ------------------------------- PID gains --------------------------------- //
// Error is in degrees; output is +/-255 PWM. Start conservative; tune on the
// bench. Kd damps overshoot from the head's inertia; Ki removes the small steady
// error the worm's stiction leaves.
const float KP = 6.0f;
const float KI = 0.8f;
const float KD = 0.6f;
const float INTEGRAL_LIMIT = 120.0f;  // anti-windup clamp on Ki*integral (PWM units)

// Deadband: within this many deg of target, stop driving (worm self-locks).
const float DEADBAND_DEG = 1.2f;
// Don't bother driving below this PWM (overcomes nothing; just buzzes).
const int MIN_DRIVE_PWM = 35;
// Hard ceiling on drive PWM (current/thermal headroom for the H-bridge).
const int MAX_DRIVE_PWM = 220;

// Stall detection: if |error| is large but the angle barely moves for this long,
// declare a stall (mechanical end-stop / jam) and back off.
const float STALL_ERR_DEG = 4.0f;
const float STALL_MOVE_DEG = 1.0f;
const unsigned long STALL_TIME_MS = 600;
// Optional current-sense stall threshold (BTS7960 IS ~ raw ADC). 0 = disabled.
const int STALL_CURRENT_ADC = 0;   // set >0 after measuring your motor's stall

// Recentre to 0 on signal loss? Safer to HOLD (worm self-locks), so default OFF.
const bool FAILSAFE_RECENTER = false;

// ------------------------------- state ------------------------------------- //
float g_targetDeg = 0.0f;       // commanded angle (clamped to range)
float g_angleDeg = 0.0f;        // measured angle
bool  g_feedbackOk = true;
float g_integral = 0.0f;
float g_prevErr = 0.0f;

unsigned long g_lastCmdMs = 0;
unsigned long g_lastTickMs = 0;
int  g_lastSeq = -1;            // heartbeat seq of the last parsed CMD (-1 = none)

// stall tracking
float g_stallRefDeg = 0.0f;
unsigned long g_stallSinceMs = 0;
bool  g_stalled = false;

char    g_line[VANCHOR_LINE_MAX];
uint8_t g_lineLen = 0;

// ----------------------------- angle reading ------------------------------- //
// Read the feedback pot -> degrees. Sets g_feedbackOk false on an implausible
// reading (open/short wiper). Light smoothing to keep PID quiet.
static float readAngleDeg() {
  int raw = analogRead(PIN_FEEDBACK_POT);
  // 3-sample median-ish smoothing (cheap, robust to single-sample noise).
  int r2 = analogRead(PIN_FEEDBACK_POT);
  int r3 = analogRead(PIN_FEEDBACK_POT);
  // median of three
  int hi = max(raw, max(r2, r3));
  int lo = min(raw, min(r2, r3));
  int med = raw + r2 + r3 - hi - lo;

  g_feedbackOk = (med >= ADC_MIN_VALID && med <= ADC_MAX_VALID);

  // Linear map ADC_AT_NEG..ADC_AT_POS  ->  -RANGE..+RANGE.
  float t = (float)(med - ADC_AT_NEG) / (float)(ADC_AT_POS - ADC_AT_NEG);
  return -STEER_RANGE_DEG + t * (2.0f * STEER_RANGE_DEG);
}

// ----------------------------- motor output -------------------------------- //
// signedPwm > 0 drives toward +angle (starboard); < 0 toward -angle (port).
// Flip the RPWM/LPWM assignment if your wiring drives the wrong way.
static void driveMotor(int signedPwm) {
  if (signedPwm > MAX_DRIVE_PWM) signedPwm = MAX_DRIVE_PWM;
  if (signedPwm < -MAX_DRIVE_PWM) signedPwm = -MAX_DRIVE_PWM;
  if (signedPwm > 0) {
    analogWrite(PIN_BTS_LPWM, 0);
    analogWrite(PIN_BTS_RPWM, signedPwm);
  } else if (signedPwm < 0) {
    analogWrite(PIN_BTS_RPWM, 0);
    analogWrite(PIN_BTS_LPWM, -signedPwm);
  } else {
    analogWrite(PIN_BTS_RPWM, 0);
    analogWrite(PIN_BTS_LPWM, 0);  // both low = coast; worm self-locks anyway
  }
}

static void brakeMotor() {
  // Coast (worm gear holds). True electrical brake would set both EN low / both
  // PWM such that the bridge shorts the motor; the self-locking worm makes that
  // unnecessary and avoids heat.
  analogWrite(PIN_BTS_RPWM, 0);
  analogWrite(PIN_BTS_LPWM, 0);
}

// --------------------------- command parsing ------------------------------- //
static void onCommand(int /*pwm*/, char /*dir*/, int steer) {
  float t = (float)steer / 100.0f;                 // -1..1
  g_targetDeg = t * STEER_RANGE_DEG;               // -> degrees
  if (g_targetDeg >  STEER_RANGE_DEG) g_targetDeg =  STEER_RANGE_DEG;
  if (g_targetDeg < -STEER_RANGE_DEG) g_targetDeg = -STEER_RANGE_DEG;
  g_lastCmdMs = millis();
}

static void pollSerial() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (g_lineLen > 0) {
        g_line[g_lineLen] = '\0';
        int pwm, steer, seq; char dir;
        if (vanchorParseCmd(g_line, &pwm, &dir, &steer, &seq)) {
          onCommand(pwm, dir, steer);
          g_lastSeq = seq;              // echo this back in the A feedback line
        }
      }
      g_lineLen = 0;
    } else if (g_lineLen < VANCHOR_LINE_MAX - 1) {
      g_line[g_lineLen++] = c;
    } else {
      g_lineLen = 0;
    }
  }
}

// ------------------------------- setup ------------------------------------- //
void setup() {
  Serial.begin(VANCHOR_BAUD);

  pinMode(PIN_BTS_RPWM, OUTPUT);
  pinMode(PIN_BTS_LPWM, OUTPUT);
  pinMode(PIN_BTS_R_EN, OUTPUT);
  pinMode(PIN_BTS_L_EN, OUTPUT);
  pinMode(PIN_STATUS_LED, OUTPUT);
  digitalWrite(PIN_BTS_R_EN, HIGH);     // enable both half-bridges
  digitalWrite(PIN_BTS_L_EN, HIGH);
  brakeMotor();

  g_angleDeg = readAngleDeg();
  g_targetDeg = g_angleDeg;             // start by holding wherever we are
  g_stallRefDeg = g_angleDeg;
  g_lastCmdMs = millis();
  g_lastTickMs = millis();
}

// ------------------------ control update (per tick) ------------------------ //
static void updateControl() {
  unsigned long now = millis();
  float dt = (now - g_lastTickMs) / 1000.0f;
  if (dt <= 0) dt = 0.001f;
  g_lastTickMs = now;

  g_angleDeg = readAngleDeg();

  bool failsafe = (now - g_lastCmdMs) > VANCHOR_WATCHDOG_MS;
  float target = g_targetDeg;
  if (failsafe) {
    if (FAILSAFE_RECENTER) target = 0.0f;
    else target = g_angleDeg;           // hold here; worm self-locks
  }
  // Soft endstops (cable wrap).
  if (target >  STEER_RANGE_DEG) target =  STEER_RANGE_DEG;
  if (target < -STEER_RANGE_DEG) target = -STEER_RANGE_DEG;

  float err = target - g_angleDeg;

  // No reliable feedback -> don't drive blind. Hold and report not-ok.
  if (!g_feedbackOk) {
    g_integral = 0.0f;
    brakeMotor();
  } else if (fabs(err) <= DEADBAND_DEG) {
    // In deadband: stop, let the worm hold. Bleed the integrator.
    g_integral *= 0.5f;
    brakeMotor();
    g_stallSinceMs = 0;
    g_stalled = false;
    g_stallRefDeg = g_angleDeg;
  } else {
    // --- PID ---
    g_integral += err * dt;
    float iTerm = KI * g_integral;
    if (iTerm >  INTEGRAL_LIMIT) { iTerm =  INTEGRAL_LIMIT; g_integral = iTerm / KI; }
    if (iTerm < -INTEGRAL_LIMIT) { iTerm = -INTEGRAL_LIMIT; g_integral = iTerm / KI; }
    float dTerm = KD * (err - g_prevErr) / dt;
    float out = KP * err + iTerm + dTerm;

    int signedPwm = (int)out;
    // Minimum-drive floor (overcome stiction) but never inside deadband.
    if (signedPwm > 0 && signedPwm < MIN_DRIVE_PWM) signedPwm = MIN_DRIVE_PWM;
    if (signedPwm < 0 && signedPwm > -MIN_DRIVE_PWM) signedPwm = -MIN_DRIVE_PWM;

    // --- stall detection (position-based, optional current-based) ---
    bool currentStall = false;
    if (STALL_CURRENT_ADC > 0) {
      int is = max(analogRead(PIN_BTS_R_IS), analogRead(PIN_BTS_L_IS));
      currentStall = (is > STALL_CURRENT_ADC);
    }
    if (fabs(err) > STALL_ERR_DEG) {
      if (fabs(g_angleDeg - g_stallRefDeg) > STALL_MOVE_DEG) {
        g_stallRefDeg = g_angleDeg;     // we are moving; reset the stall clock
        g_stallSinceMs = now;
        g_stalled = false;
      } else {
        if (g_stallSinceMs == 0) g_stallSinceMs = now;
        if ((now - g_stallSinceMs) > STALL_TIME_MS || currentStall) g_stalled = true;
      }
    } else {
      g_stallSinceMs = 0;
      g_stalled = false;
      g_stallRefDeg = g_angleDeg;
    }

    if (g_stalled) {
      // Hung on an end-stop / jam: stop pushing, dump integrator (anti-windup).
      g_integral = 0.0f;
      brakeMotor();
    } else {
      driveMotor(signedPwm);
    }
  }
  g_prevErr = err;

  // status LED: solid if feedback lost or stalled, else heartbeat.
  if (!g_feedbackOk || g_stalled) digitalWrite(PIN_STATUS_LED, HIGH);
  else digitalWrite(PIN_STATUS_LED, (now / 500) & 1);

  // --- report actual angle back to the Pi (closed-loop telemetry) --- //
  static unsigned long lastReport = 0;
  if (now - lastReport >= 100) {        // ~10 Hz feedback
    lastReport = now;
    int wrap = (int)(g_angleDeg / STEER_RANGE_DEG * 100.0f);
    if (wrap > 100) wrap = 100;
    if (wrap < -100) wrap = -100;
    Serial.print("A ");
    Serial.print(g_angleDeg, 1);
    Serial.print(g_feedbackOk ? " 1 " : " 0 ");
    Serial.print(wrap);
    Serial.print(' ');
    Serial.print(g_lastSeq);      // heartbeat echo (roadmap #18)
    Serial.print("\r\n");
  }
}

// ------------------------------- loop -------------------------------------- //
void loop() {
  pollSerial();
  updateControl();
}
