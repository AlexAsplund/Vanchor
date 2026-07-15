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

// Assert a line parses OK and yields the expected pwm/dir/steer AND seq, when
// the optional 5th out-parameter is requested (heartbeat, roadmap #18).
static void expectOkSeq(const char *line, int wpwm, char wdir, int wsteer,
                        int wseq) {
  int pwm = -1, steer = 999, seq = -999;
  char dir = '?';
  bool ok = vanchorParseCmd(line, &pwm, &dir, &steer, &seq);
  CHECK(ok);
  CHECK(pwm == wpwm);
  CHECK(dir == wdir);
  CHECK(steer == wsteer);
  CHECK(seq == wseq);
  if (!ok || pwm != wpwm || dir != wdir || steer != wsteer || seq != wseq) {
    std::printf("  ^ line=\"%s\" got ok=%d pwm=%d dir=%c steer=%d seq=%d\n",
                line, ok, pwm, dir, steer, seq);
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

static void runCrcVectors();  // protocol v2 vectors (defined below)

// Assert a STEERD line parses OK and yields the expected degrees (± tolerance
// for the float accumulation) and seq.
static void expectSteerDeg(const char *line, float wdeg, int wseq) {
  float deg = -9999.0f;
  int seq = -999;
  bool ok = vanchorParseSteerDeg(line, &deg, &seq);
  CHECK(ok);
  CHECK(deg > wdeg - 0.01f && deg < wdeg + 0.01f);
  CHECK(seq == wseq);
  if (!ok || !(deg > wdeg - 0.01f && deg < wdeg + 0.01f) || seq != wseq) {
    std::printf("  ^ line=\"%s\" got ok=%d deg=%.2f seq=%d\n", line, ok, deg, seq);
  }
}

static void expectSteerDegReject(const char *line) {
  const float SENT = 12.5f;
  float deg = SENT;
  bool ok = vanchorParseSteerDeg(line, &deg);
  CHECK(!ok);
  CHECK(deg == SENT);   // outputs untouched on rejection
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

  // ---- Heartbeat seq echo (roadmap #18) --------------------------------
  // Backward compatibility: the 4-arg overload still works AND a line without a
  // seq field is accepted exactly as before (existing expectOk cases above).
  // A CMD with no seq field, but seq requested, yields -1 ("no seq present").
  expectOkSeq("CMD 100 F 50", 100, 'F', 50, -1);
  expectOkSeq("CMD 0 F 0\r", 0, 'F', 0, -1);
  // A CMD WITH a seq field fills it; whitespace and CRLF still tolerated.
  expectOkSeq("CMD 100 F 50 7", 100, 'F', 50, 7);
  expectOkSeq("CMD 255 R -100 42", 255, 'R', -100, 42);
  expectOkSeq("CMD 10 F 5 0", 10, 'F', 5, 0);          // seq 0 is valid, != -1
  expectOkSeq("CMD 10 F 5   99\r\n", 10, 'F', 5, 99);  // extra spaces + CRLF
  expectOkSeq("CMD 300 R 250 123", 255, 'R', 100, 123);// pwm/steer clamp, seq kept
  // Seq clamps at VANCHOR_SEQ_MAX so a giant value never overflows.
  expectOkSeq("CMD 10 F 5 999999", 10, 'F', 5, VANCHOR_SEQ_MAX);
  // A non-numeric tail after a valid steer is ignored -> seq -1, still accepted
  // (must never turn a valid command into a rejection).
  expectOkSeq("CMD 10 F 5 abc", 10, 'F', 5, -1);
  // Seq has no sign support (heartbeat seqs are non-negative); a "-1" tail is
  // treated as an ignored non-numeric tail -> seq -1, command still valid.
  expectOkSeq("CMD 10 F 5 -1", 10, 'F', 5, -1);

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

  // ---- STEERD (v2.1: split steering channel, degrees-native) -----------
  expectSteerDeg("STEERD 0.0", 0.0f, -1);
  expectSteerDeg("STEERD -35.0", -35.0f, -1);
  expectSteerDeg("STEERD 180.0", 180.0f, -1);
  expectSteerDeg("STEERD 95.5", 95.5f, -1);
  expectSteerDeg("STEERD +12.25", 12.25f, -1);
  expectSteerDeg("STEERD 42", 42.0f, -1);            // integer degrees OK
  expectSteerDeg("STEERD   -7.5  ", -7.5f, -1);      // whitespace tolerated
  expectSteerDeg("STEERD -35.0 42", -35.0f, 42);     // heartbeat seq
  expectSteerDeg("STEERD 10.0 999999", 10.0f, VANCHOR_SEQ_MAX);  // seq clamps
  expectSteerDeg("STEERD 9000.0", 720.0f, -1);       // sanity clamp ±720
  expectSteerDegReject("STEERD");                    // no value
  expectSteerDegReject("STEERD ");                   // no digits
  expectSteerDegReject("STEERD abc");                // non-numeric
  expectSteerDegReject("STEERD -");                  // sign only
  expectSteerDegReject("STEERD 12.");                // dangling decimal point
  expectSteerDegReject("STEERDX 10");                // header must be exact
  expectSteerDegReject("STEER -50");                 // dead pre-v2.1 token
  expectSteerDegReject("CMD 0 F 0");                 // not a STEERD line
  // A CMD line must NOT parse as STEERD and vice versa (dispatch order safe).
  {
    int pwm; char dir; int steer;
    CHECK(!vanchorParseCmd("STEERD -35.0", &pwm, &dir, &steer));
  }

  // Protocol v2 CRC vectors — ALWAYS run (previously only reachable when the
  // parse checks above had already failed, so CI never exercised them).
  runCrcVectors();

  if (g_failures == 0) {
    std::printf("OK: all %d checks passed\n", g_checks);
    return 0;
  }
  std::printf("FAILED: %d of %d checks failed\n", g_failures, g_checks);
  return 1;
}

// ---- protocol v2: CRC vectors shared with the Python suite ---------------
// Reads ../../common/protocol_vectors.txt and checks vanchorCheckCrc() +
// vanchorAcceptLine() agree with the recorded verdicts. Keeping ONE vector
// file for both suites means the two CRC implementations cannot drift.
static void runCrcVectors() {
  FILE *f = std::fopen("../../common/protocol_vectors.txt", "r");
  if (!f) { std::printf("FAIL cannot open protocol_vectors.txt\n"); ++g_failures; return; }
  char raw[160];
  while (std::fgets(raw, sizeof raw, f)) {
    if (raw[0] == '#' || raw[0] == '\n') continue;
    char verdict[8], line[144];
    if (std::sscanf(raw, "%7s %[^\n]", verdict, line) != 2) continue;
    char work[144];
    std::strcpy(work, line);
    int v = vanchorCheckCrc(work);
    if (std::strcmp(verdict, "OK") == 0) {
      CHECK(v == 1);
      char work2[144]; std::strcpy(work2, line);
      CHECK(vanchorAcceptLine(work2));
    } else if (std::strcmp(verdict, "BAD") == 0) {
      CHECK(v == 0);
      char work2[144]; std::strcpy(work2, line);
      CHECK(!vanchorAcceptLine(work2));
    } else {  // NOCRC: absent suffix; acceptance depends on VANCHOR_REQUIRE_CRC
      CHECK(v == -1);
      char work2[144]; std::strcpy(work2, line);
#if VANCHOR_REQUIRE_CRC
      CHECK(!vanchorAcceptLine(work2));
#else
      CHECK(vanchorAcceptLine(work2));
#endif
    }
  }
  std::fclose(f);
  // A stripped OK line must still parse as a command when it is a CMD.
  char cmd[64] = "CMD 128 R -100 42*7D";
  int pwm; char dir; int steer, seq;
  CHECK(vanchorAcceptLine(cmd));
  CHECK(vanchorParseCmd(cmd, &pwm, &dir, &steer, &seq));
  CHECK(pwm == 128 && dir == 'R' && steer == -100 && seq == 42);
  // ...and a stripped OK STEERD line must parse as degrees + seq.
  char sd[64] = "STEERD -35.0 42*82";
  float deg; int sdseq;
  CHECK(vanchorAcceptLine(sd));
  CHECK(vanchorParseSteerDeg(sd, &deg, &sdseq));
  CHECK(deg > -35.01f && deg < -34.99f && sdseq == 42);
  // Round-trip the append helper against a known value.
  char out[64] = "A -12.4 1 -7 42";
  vanchorAppendCrc(out, sizeof out);
  CHECK(std::strcmp(out, "A -12.4 1 -7 42*C8") == 0);
}
