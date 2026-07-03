// Host-compiled unit test for vanchorParseCmd() from the shared firmware
// protocol header (firmware/common/vanchor_protocol.h).
//
// The firmware runs on an Arduino, but the command parser is plain, portable
// C that has no Arduino dependencies, so we compile it with the host g++ and
// exercise it against valid + malformed command lines. This catches parser
// regressions in CI without needing the AVR toolchain or hardware.
//
// Build + run:  make        (see the sibling Makefile)
//               ./test_parse_cmd

#include <cstdio>
#include <cstring>

#include "../../common/vanchor_protocol.h"

static int g_failures = 0;
static int g_checks = 0;

#define CHECK(cond)                                                        \
  do {                                                                     \
    ++g_checks;                                                            \
    if (!(cond)) {                                                         \
      ++g_failures;                                                        \
      std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);          \
    }                                                                      \
  } while (0)

// Assert a line parses OK and yields the expected pwm/dir/steer.
static void expectOk(const char *line, int wpwm, char wdir, int wsteer) {
  int pwm = -1, steer = 999;
  char dir = '?';
  bool ok = vanchorParseCmd(line, &pwm, &dir, &steer);
  CHECK(ok);
  CHECK(pwm == wpwm);
  CHECK(dir == wdir);
  CHECK(steer == wsteer);
  if (!ok || pwm != wpwm || dir != wdir || steer != wsteer) {
    std::printf("  ^ line=\"%s\" got ok=%d pwm=%d dir=%c steer=%d\n",
                line, ok, pwm, dir, steer);
  }
}

// Assert a line is rejected AND leaves the caller's outputs untouched (the
// header contract: "Leaves outputs untouched + returns false on any malformed
// line so the caller keeps the last good command").
static void expectReject(const char *line) {
  const int SENT_PWM = 42;
  const int SENT_STEER = -7;
  const char SENT_DIR = 'Z';
  int pwm = SENT_PWM, steer = SENT_STEER;
  char dir = SENT_DIR;
  bool ok = vanchorParseCmd(line, &pwm, &dir, &steer);
  CHECK(!ok);
  CHECK(pwm == SENT_PWM);
  CHECK(dir == SENT_DIR);
  CHECK(steer == SENT_STEER);
  if (ok) std::printf("  ^ line=\"%s\" was accepted but should be rejected\n", line);
}

int main() {
  // ---- Well-formed lines from the header's own examples ----------------
  expectOk("CMD 0 F 0", 0, 'F', 0);       // stopped, centred
  expectOk("CMD 255 F 0", 255, 'F', 0);   // full ahead, centred
  expectOk("CMD 128 R -100", 128, 'R', -100);  // half astern, hard port

  // ---- Steering sign + explicit '+' ------------------------------------
  expectOk("CMD 60 F 100", 60, 'F', 100);
  expectOk("CMD 60 F +25", 60, 'F', 25);
  expectOk("CMD 60 R -1", 60, 'R', -1);

  // ---- Whitespace tolerance (leading, extra internal, trailing CR) -----
  expectOk("   CMD 10 F 5", 10, 'F', 5);
  expectOk("CMD   64   R   -30", 64, 'R', -30);
  expectOk("CMD 200 F 50\r", 200, 'F', 50);   // trailing '\r' tolerated
  expectOk("CMD 200 F 50\r\n", 200, 'F', 50); // trailing CRLF tolerated

  // ---- Clamping to the documented ranges -------------------------------
  expectOk("CMD 999 F 0", 255, 'F', 0);       // pwm clamps at 255
  expectOk("CMD 300 R 250", 255, 'R', 100);   // pwm + steer both clamp high
  expectOk("CMD 50 F -250", 50, 'F', -100);   // steer clamps low

  // ---- Malformed / garbage: must be rejected, outputs untouched --------
  expectReject("");                 // empty line
  expectReject("   ");              // whitespace only
  expectReject("\r\n");             // bare line ending
  expectReject("C");                // truncated header
  expectReject("CM");               // truncated header
  expectReject("CMX 1 F 0");        // wrong header
  expectReject("XYZ 1 F 0");        // wrong header
  expectReject("cmd 1 F 0");        // case-sensitive header
  expectReject("CMD");              // no fields
  expectReject("CMD F 0");          // missing pwm digits
  expectReject("CMD -5 F 0");       // pwm has no leading-sign support
  expectReject("CMD abc F 0");      // non-numeric pwm
  expectReject("CMD 100 X 0");      // invalid direction
  expectReject("CMD 100 f 0");      // direction is case-sensitive
  expectReject("CMD 100 0");        // missing direction
  expectReject("CMD 100 F");        // missing steer
  expectReject("CMD 100 F -");      // sign with no steer digits
  expectReject("CMD 100 F +");      // sign with no steer digits
  expectReject("CMD 100 F abc");    // non-numeric steer
  expectReject("!@#$%^&*()");       // pure garbage
  expectReject("$GPRMC,123519,A");  // an NMEA sentence is not a CMD

  if (g_failures == 0) {
    std::printf("OK: all %d checks passed\n", g_checks);
    return 0;
  }
  std::printf("FAILED: %d of %d checks failed\n", g_failures, g_checks);
  return 1;
}
