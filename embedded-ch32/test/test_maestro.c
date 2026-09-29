/* Host test for User/maestro.c (run: make hosttest), compiled unmodified
   against stubbed servo, sense and script modules and a hand-driven clock.
   Each scenario runs in a fresh process (maestro.c keeps static state),
   selected by argv[1]. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "maestro.h"
#include "servo.h"

/* --- stubs --------------------------------------------------------------------- */

static uint16_t g_t[SERVO_CHANNELS];               /* output, 0.5 us ticks */
static uint16_t g_min[SERVO_CHANNELS], g_max[SERVO_CHANNELS];   /* limits, us */
static bool     g_on[SERVO_CHANNELS];
#define US(ch)  (g_t[ch] / 2u)
static uint32_t g_ms;
static uint8_t  g_faults;
static int      g_script_stops;
static uint8_t  g_reply[8];
static uint16_t g_reply_len;

static void limits_default(void)
{
    int ch;
    for (ch = 0; ch < (int)SERVO_CHANNELS; ch++) { g_min[ch] = SERVO_US_MIN; g_max[ch] = SERVO_US_MAX; }
}
bool Servo_SetPulseTicks(uint8_t ch, uint16_t t)
{
    if (t < g_min[ch] * 2u) { t = (uint16_t)(g_min[ch] * 2u); }
    if (t > g_max[ch] * 2u) { t = (uint16_t)(g_max[ch] * 2u); }
    g_t[ch] = t; g_on[ch] = true; return true;
}
uint16_t Servo_GetPulseTicks(uint8_t ch)       { return g_on[ch] ? g_t[ch] : 0u; }
void Servo_GetLimitsUs(uint8_t ch, uint16_t *lo, uint16_t *hi) { *lo = g_min[ch]; *hi = g_max[ch]; }
bool Servo_SetEnabled(uint8_t ch, bool e)      { g_on[ch] = e; return true; }
bool Servo_IsEnabled(uint8_t ch)               { return g_on[ch]; }
void Servo_DisableAll(void)                    { memset(g_on, 0, sizeof g_on); }
uint32_t Servo_Millis(void)                    { return g_ms; }
uint8_t Sense_Faults(void)                     { return g_faults; }
void Script_Stop(void)                         { g_script_stops++; }
bool Script_Running(void)                      { return false; }

static void send(const uint8_t *d, uint16_t n)
{
    memcpy(g_reply + g_reply_len, d, n);
    g_reply_len = (uint16_t)(g_reply_len + n);
}

/* --- helpers ------------------------------------------------------------------- */

static int failures;
#define CHECK(cond, ...) do { if (!(cond)) { printf("  FAIL line %d: ", __LINE__); \
    printf(__VA_ARGS__); printf("\n"); failures++; } } while (0)

static int feed(const uint8_t *b, int n)
{
    int ascii = 0, i;
    for (i = 0; i < n; i++)
    {
        if (!Maestro_Feed(b[i], send)) { ascii++; }
    }
    return ascii;
}
#define FEED(...) do { const uint8_t b_[] = { __VA_ARGS__ }; feed(b_, (int)sizeof b_); } while (0)

static void run_ms(uint32_t ms)
{
    while (ms--) { g_ms++; Maestro_Task(); }
}

static void set_target(uint8_t ch, uint16_t q) { FEED(0x84, ch, q & 0x7F, (q >> 7) & 0x7F); }

static uint16_t get_position(uint8_t ch)
{
    g_reply_len = 0;
    FEED(0x90, ch);
    CHECK(g_reply_len == 2, "Get Position reply is %u bytes", g_reply_len);
    return (uint16_t)(g_reply[0] | (g_reply[1] << 8));
}

static uint8_t moving(void)
{
    g_reply_len = 0;
    FEED(0x93);
    CHECK(g_reply_len == 1, "Get Moving State reply is %u bytes", g_reply_len);
    return g_reply[0];
}

static uint16_t errors(void)
{
    g_reply_len = 0;
    FEED(0xA1);
    return (uint16_t)(g_reply[0] | (g_reply[1] << 8));
}

/* --- scenarios ----------------------------------------------------------------- */

static void s1_compact_target(void)
{
    set_target(0, 6000);
    CHECK(g_on[0] && US(0) == 1500, "ch0 on=%d us=%u", g_on[0], US(0));
    CHECK(get_position(0) == 6000, "position %u", get_position(0));
    CHECK(moving() == 0, "not moving without limits");
    set_target(0, 7000);
    CHECK(US(0) == 1750, "us=%u", US(0));
    set_target(0, 6001);                           /* not a whole microsecond */
    CHECK(US(0) == 1500 && get_position(0) == 6001,
          "exact target read back once arrived: us=%u pos=%u", US(0), get_position(0));
    FEED(0x87, 0, 7, 0);
    set_target(0, 7003);
    run_ms(3000);
    CHECK(get_position(0) == 7003 && moving() == 0, "after a ramp too: pos=%u", get_position(0));
}

static void s2_pololu_device(void)
{
    FEED(0xAA, 12, 0x04, 3, 7000 & 0x7F, 7000 >> 7);
    CHECK(g_on[3] && US(3) == 1750, "device 12: us=%u", US(3));
    FEED(0xAA, 13, 0x04, 3, 4000 & 0x7F, 4000 >> 7);
    CHECK(US(3) == 1750, "device 13 must be ignored, us=%u", US(3));
    g_reply_len = 0;
    FEED(0xAA, 12, 0x10, 3);                       /* Pololu Get Position */
    CHECK(g_reply_len == 2 && (g_reply[0] | (g_reply[1] << 8)) == 7000, "pololu get position");
    CHECK(errors() == 0, "no errors");
}

static void s3_ascii_passes(void)
{
    const uint8_t line[] = "S 0 1500\n";
    CHECK(feed(line, (int)sizeof line - 1) == (int)sizeof line - 1, "ASCII bytes must not be consumed");
    CHECK(!g_on[0], "ASCII must not move anything");
}

static void s4_speed(void)
{
    set_target(0, 6000);                           /* 1500 us, from off: immediate */
    FEED(0x87, 0, 10, 0);                          /* 10 qus / 10 ms = 250 us/s    */
    set_target(0, 8000);                           /* +500 us -> 2.0 s             */
    CHECK(moving() == 1, "moving");
    run_ms(1000);
    CHECK(US(0) >= 1745 && US(0) <= 1755, "after 1 s: %u us (want ~1750)", US(0));
    CHECK(get_position(0) >= 6980 && get_position(0) <= 7020, "position %u", get_position(0));
    run_ms(1010);
    CHECK(US(0) == 2000 && moving() == 0, "done: %u us moving=%u", US(0), moving());
}

static void s5_accel(void)
{
    uint16_t prev, at_quarter, at_three_quarters;
    uint32_t t = 0;

    set_target(1, 6000);
    FEED(0x89, 1, 4, 0);                           /* accel 4, speed unlimited */
    set_target(1, 8000);
    prev = 1500;
    at_quarter = at_three_quarters = 0;
    while (moving() && t < 5000)
    {
        run_ms(10);
        t += 10;
        CHECK(US(1) >= prev, "never moves backwards (%u < %u at %u ms)", US(1), prev, t);
        prev = US(1);
        if (!at_quarter && US(1) >= 1625) { at_quarter = (uint16_t)t; }
        if (!at_three_quarters && US(1) >= 1875) { at_three_quarters = (uint16_t)t; }
    }
    CHECK(US(1) == 2000, "reached target: %u", US(1));
    /* accelerating then decelerating: the middle half is covered faster than
       the first quarter plus the last quarter would suggest at constant speed */
    CHECK(at_quarter > 0 && (at_three_quarters - at_quarter) < at_quarter * 2u,
          "S-curve: 1/4 at %u ms, 3/4 at %u ms, end %u ms", at_quarter, at_three_quarters, t);
    CHECK(t > 300 && t < 2000, "duration %u ms", t);
}

static void s6_fault_interlock(void)
{
    g_faults = 1;
    set_target(2, 6000);
    CHECK(!g_on[2], "a latched fault must stop a target energising the channel");
    g_faults = 0;
    set_target(2, 6000);
    FEED(0x87, 2, 5, 0);
    set_target(2, 8000);
    run_ms(200);
    Servo_DisableAll();                            /* X or a rail trip mid-move */
    run_ms(500);
    CHECK(!g_on[2], "a ramp must never re-enable a channel that was switched off");
    CHECK(moving() == 0, "and it no longer counts as moving");
}

static void s7_errors(void)
{
    FEED(0x80);                                    /* unknown command            */
    CHECK(errors() == MAESTRO_ERR_PROTOCOL, "unknown command -> protocol error");
    CHECK(errors() == 0, "Get Errors clears");
    FEED(0x84, 0, 0x70);                           /* truncated Set Target...    */
    set_target(0, 6000);                           /* ...then a complete one     */
    CHECK(errors() == MAESTRO_ERR_PROTOCOL, "truncated command -> protocol error");
    CHECK(g_on[0] && US(0) == 1500, "the next command still works: us=%u", US(0));
}

static void s8_multiple_and_off(void)
{
    FEED(0x9F, 3, 4, 6000 & 0x7F, 6000 >> 7, 7000 & 0x7F, 7000 >> 7, 5000 & 0x7F, 5000 >> 7);
    CHECK(US(4) == 1500 && US(5) == 1750 && US(6) == 1250, "multi: %u %u %u", US(4), US(5), US(6));
    set_target(5, 0);
    CHECK(!g_on[5] && get_position(5) == 0, "target 0 turns the channel off");
    FEED(0xA2);
    CHECK(!g_on[4] && !g_on[6] && g_script_stops == 1, "Go Home: everything off, script stopped");
}

static void s9_mini_ssc_and_clamp(void)
{
    FEED(0xFF, 0, 127);
    CHECK(US(0) == 1500, "mini ssc 127 -> %u", US(0));
    FEED(0xFF, 0, 254);
    CHECK(US(0) >= 1975 && US(0) <= 1977, "mini ssc 254 -> %u", US(0));
    FEED(0xFF, 0, 0);
    CHECK(US(0) >= 1023 && US(0) <= 1025, "mini ssc 0 -> %u", US(0));
    set_target(1, 12000);                          /* 3000 us */
    CHECK(US(1) == SERVO_US_MAX, "clamped high: %u", US(1));
    set_target(1, 1000);                           /* 250 us */
    CHECK(US(1) == SERVO_US_MIN, "clamped low: %u", US(1));
}

static void s10_retarget_and_ascii_takeover(void)
{
    set_target(0, 6000);
    FEED(0x87, 0, 20, 0);
    set_target(0, 8000);
    run_ms(500);
    set_target(0, 6000);                           /* reverse mid-move */
    run_ms(2000);
    CHECK(US(0) == 1500 && moving() == 0, "reversed back to 1500: %u", US(0));
    set_target(0, 8000);
    run_ms(100);
    Maestro_Cancel(0);                             /* ASCII 'S' took the channel */
    g_t[0] = 2400;
    run_ms(500);
    CHECK(US(0) == 1200, "a cancelled ramp leaves the channel alone: %u", US(0));
    CHECK(get_position(0) == 4800, "position follows what is really output: %u", get_position(0));
}

static void s11_half_us_and_limits(void)
{
    set_target(0, 6002);                           /* 1500.5 us */
    CHECK(g_t[0] == 3001 && get_position(0) == 6002, "0.5 us output: ticks=%u pos=%u", g_t[0], get_position(0));
    set_target(0, 6001);                           /* 1500.25 us: nearest tick */
    CHECK(g_t[0] == 3001 || g_t[0] == 3000, "quarter-us rounds to a tick: %u", g_t[0]);
    CHECK(get_position(0) == 6001, "and still reads back exactly: %u", get_position(0));

    g_min[1] = 1000; g_max[1] = 2000;              /* narrower limits */
    set_target(1, 9000);
    CHECK(US(1) == 2000, "clamped to channel max: %u", US(1));
    set_target(1, 3000);
    CHECK(US(1) == 1000, "clamped to channel min: %u", US(1));

    g_min[2] = SERVO_US_ABS_MIN; g_max[2] = SERVO_US_ABS_MAX;   /* widened */
    set_target(2, 16000);
    CHECK(US(2) == 4000, "4000 us allowed once widened: %u", US(2));
    set_target(2, 400);
    CHECK(US(2) == 100, "100 us allowed once widened: %u", US(2));
}

int main(int argc, char **argv)
{
    int s = (argc > 1) ? atoi(argv[1]) : 0;

    limits_default();

    switch (s)
    {
        case 1:  s1_compact_target(); break;
        case 2:  s2_pololu_device(); break;
        case 3:  s3_ascii_passes(); break;
        case 4:  s4_speed(); break;
        case 5:  s5_accel(); break;
        case 6:  s6_fault_interlock(); break;
        case 7:  s7_errors(); break;
        case 8:  s8_multiple_and_off(); break;
        case 9:  s9_mini_ssc_and_clamp(); break;
        case 10: s10_retarget_and_ascii_takeover(); break;
        case 11: s11_half_us_and_limits(); break;
        default: printf("unknown scenario %d\n", s); return 2;
    }
    printf("maestro scenario %d: %s\n", s, failures ? "FAILED" : "ok");
    return failures ? 1 : 0;
}
