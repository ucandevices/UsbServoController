/********************************************************************************
 * protocol.c -- ASCII command protocol over USB CDC, CH32V203C8T6 port.
 *
 * Command set and parsing are byte-for-byte the same as the STM32 build; only
 * the transport call and the bootloader entry differ.
 ********************************************************************************/

#include "protocol.h"
#include "servo.h"
#include "sense.h"
#include "cdc_glue.h"
#include "usb_lib.h"
#include "usb_desc.h"
#include "hw_config.h"
#include "bootflag.h"
#include "script.h"
#include "maestro.h"
#include "settings.h"

#include <stdio.h>
#include <string.h>

#define RX_RING_SIZE  256u
#define LINE_MAX       64u

static volatile uint8_t  s_ring[RX_RING_SIZE];
static volatile uint16_t s_head;
static volatile uint16_t s_tail;

static char    s_line[LINE_MAX];
static uint8_t s_line_len;
static uint8_t s_boot_requested;

/* ------------------------------------------------------------------------- */

void Protocol_RxData(const uint8_t *data, uint32_t len)
{
    uint32_t i;
    for (i = 0; i < len; i++)
    {
        uint16_t next = (uint16_t)((s_head + 1u) % RX_RING_SIZE);
        if (next == s_tail)
        {
            break;              /* ring full -- drop the rest of this packet */
        }
        s_ring[s_head] = data[i];
        s_head = next;
    }
}

static void send_raw(const uint8_t *data, uint16_t len)
{
    uint32_t guard;

    for (guard = 0; guard < 100000u; guard++)
    {
        if (USBD_ENDPx_DataUp(ENDP3, (uint8_t *)data, len) == USB_SUCCESS)
        {
            return;
        }
    }
}

static void reply(const char *s)
{
    send_raw((const uint8_t *)s, (uint16_t)strlen(s));
}

static void reply_ok(void)           { reply("OK\r\n"); }
static void reply_err(const char *r) { char b[48]; snprintf(b, sizeof b, "ERR %s\r\n", r); reply(b); }

static void reply_val(uint32_t v)
{
    char b[32];
    snprintf(b, sizeof b, "OK %lu\r\n", (unsigned long)v);
    reply(b);
}

static void reply_fault(void)
{
    char b[64];
    snprintf(b, sizeof b, "OK %u %lu %lu %u\r\n",
             (unsigned)Sense_Faults(),
             (unsigned long)Sense_LastCurrent_mA(),
             (unsigned long)Sense_LastVoltage_mV(),
             (unsigned)(Sense_RailPresent() ? 1u : 0u));
    reply(b);
}

static void reply_travel(uint8_t ch)
{
    char b[32];
    uint16_t lo, hi;

    Servo_GetLimitsUs(ch, &lo, &hi);
    snprintf(b, sizeof b, "OK %u %u\r\n", (unsigned)lo, (unsigned)hi);
    reply(b);
}

static void reply_limits(void)
{
    char b[48];
    uint32_t ma, mv;

    Sense_GetLimits(&ma, &mv);
    snprintf(b, sizeof b, "OK %lu %lu %u\r\n",
             (unsigned long)ma, (unsigned long)mv,
             (unsigned)(Sense_UndervoltageAuto() ? 1u : 0u));
    reply(b);
}

static uint8_t parse_u32(const char **p, uint32_t *out)
{
    const char *s = *p;
    uint32_t v = 0;
    uint8_t digits = 0;

    while (*s == ' ' || *s == '\t') { s++; }
    while (*s >= '0' && *s <= '9')
    {
        v = (v * 10u) + (uint32_t)(*s - '0');
        s++;
        digits++;
        if (digits > 9u) { return 0; }
    }
    if (digits == 0u) { return 0; }

    *p = s;
    *out = v;
    return 1;
}

static int str_eq_nocase(const char *a, const char *b, uint32_t n)
{
    uint32_t i;
    for (i = 0; i < n; i++)
    {
        char x = a[i], y = b[i];
        if (x >= 'a' && x <= 'z') { x = (char)(x - 'a' + 'A'); }
        if (y >= 'a' && y <= 'z') { y = (char)(y - 'a' + 'A'); }
        if (x != y) { return 0; }
        if (x == '\0') { break; }
    }
    return 1;
}

static void execute(char *line)
{
    const char *p = line;
    char verb;
    uint32_t a = 0, b = 0;
    uint32_t u32 = 0;
    uint16_t v16 = 0, lo = 0, hi = 0;

    while (*p == ' ' || *p == '\t') { p++; }
    if (*p == '\0') { return; }

    verb = *p;
    if (verb >= 'a' && verb <= 'z') { verb = (char)(verb - 'a' + 'A'); }

    if (verb == 'B' && str_eq_nocase(p, "BOOT", 4))
    {
        reply("OK BOOT\r\n");
        Delay_Ms(50);
        s_boot_requested = 1u;
        return;
    }

    p++;

    switch (verb)
    {
        case 'S':
            if (!parse_u32(&p, &a) || !parse_u32(&p, &b)) { reply_err("syntax"); break; }
            if (a >= SERVO_CHANNELS)                      { reply_err("channel"); break; }
            Servo_GetLimitsUs((uint8_t)a, &lo, &hi);
            if (b < lo || b > hi)                         { reply_err("range");   break; }
            /* Servo_SetPulseUs() enables the channel, so 'S' is a second way to
               re-energise a servo and must honour the same interlock as 'E' --
               otherwise a latched fault stops nothing. */
            if (Sense_Faults() != 0u)                     { reply_err("fault");   break; }
            Maestro_Cancel((uint8_t)a);
            Servo_SetPulseUs((uint8_t)a, (uint16_t)b);
            reply_ok();
            break;

        case 'G':
            if (!parse_u32(&p, &a))  { reply_err("syntax");  break; }
            if (a >= SERVO_CHANNELS) { reply_err("channel"); break; }
            reply_val(Servo_GetPulseUs((uint8_t)a));
            break;

        case 'E':
            if (!parse_u32(&p, &a) || !parse_u32(&p, &b)) { reply_err("syntax"); break; }
            if (a >= SERVO_CHANNELS)                      { reply_err("channel"); break; }
            /* Refuse to re-energise anything while a rail fault is latched;
               clear it with 'C' once the cause is understood. */
            if (b != 0u && Sense_Faults() != 0u)          { reply_err("fault");   break; }
            Maestro_Cancel((uint8_t)a);
            Servo_SetEnabled((uint8_t)a, b != 0u);
            reply_ok();
            break;

        case 'A':
            if (!parse_u32(&p, &a))           { reply_err("syntax");   break; }
            if (!Servo_HasAnalog((uint8_t)a)) { reply_err("noanalog"); break; }
            if (!Servo_ReadAnalog((uint8_t)a, &v16)) { reply_err("adc"); break; }
            reply_val(v16);
            break;

        case 'I':   /* rail current, mA */
            if (!Sense_ReadCurrent_mA(&u32)) { reply_err("adc"); break; }
            reply_val(u32);
            break;

        case 'U':   /* rail voltage, mV */
            if (!Sense_ReadVoltage_mV(&u32)) { reply_err("adc"); break; }
            reply_val(u32);
            break;

        case 'N':   /* raw sense counts: 0 = I_SENSE, 1 = V_SENSE */
            if (!parse_u32(&p, &a))           { reply_err("syntax"); break; }
            if (a > SENSE_RAW_VOLTAGE)        { reply_err("index");  break; }
            if (!Sense_ReadRaw((uint8_t)a, &v16)) { reply_err("adc"); break; }
            reply_val(v16);
            break;

        case 'Z':   /* zero the current sense (disables all channels first) */
            if (!Sense_TareCurrent(&v16)) { reply_err("adc"); break; }
            reply_val(v16);
            break;

        case 'F':   /* faults, last readings, rail-present flag */
            reply_fault();
            break;

        case 'C':   /* clear latched faults */
            Sense_ClearFaults();
            reply_ok();
            break;

        case 'P':   /* set protection limits: P <mA> <mV>, 0 disables either */
            if (!parse_u32(&p, &a) || !parse_u32(&p, &b)) { reply_err("syntax"); break; }
            Sense_SetLimits(a, b);
            reply_ok();
            break;

        case 'L':   /* limits in force: OK <mA> <mV> <1 = undervoltage auto> */
            reply_limits();
            break;

        case 'X':
            Script_Stop();
            Maestro_CancelAll();
            Servo_DisableAll();
            reply_ok();
            break;

        case 'V':
            reply(PROTO_VERSION_STRING "\r\n");
            break;

        case 'R':   /* travel limits: R <ch> reads, R <ch> <min> <max> sets */
            if (!parse_u32(&p, &a))  { reply_err("syntax");  break; }
            if (a >= SERVO_CHANNELS) { reply_err("channel"); break; }
            if (!parse_u32(&p, &b))  { reply_travel((uint8_t)a); break; }
            if (!parse_u32(&p, &u32)) { reply_err("syntax"); break; }
            if (b > 0xFFFFu || u32 > 0xFFFFu ||
                !Servo_SetLimitsUs((uint8_t)a, (uint16_t)b, (uint16_t)u32))
            {
                reply_err("range");
                break;
            }
            reply_ok();
            break;

        case 'W':   /* W saves settings to flash, W D restores factory defaults */
            while (*p == ' ' || *p == '\t') { p++; }
            if (*p == 'D' || *p == 'd') { if (Settings_Reset()) { reply_ok(); } else { reply_err("flash"); } }
            else if (*p == '\0')        { if (Settings_Save())  { reply_ok(); } else { reply_err("flash"); } }
            else                        { reply_err("syntax"); }
            break;

        case 'Q':   /* stored script: QC QA QS QI QG QR QX, see script.h */
        {
            char out[48];
            Script_Command(p, out, sizeof out);
            reply(out);
            break;
        }

        default:
            reply_err("unknown");
            break;
    }
}

void Protocol_Task(void)
{
    while (s_tail != s_head)
    {
        uint8_t c = s_ring[s_tail];
        s_tail = (uint16_t)((s_tail + 1u) % RX_RING_SIZE);

        /* Bytes of 0x80 and above start a Pololu Maestro command (maestro.h);
           the parser keeps its data bytes too. Everything else is ASCII. */
        if (Maestro_Feed(c, send_raw))
        {
            continue;
        }

        if (c == '\r')
        {
            continue;
        }

        if (c == '\n')
        {
            s_line[s_line_len] = '\0';
            execute(s_line);
            s_line_len = 0u;
            continue;
        }

        if (s_line_len < (LINE_MAX - 1u))
        {
            s_line[s_line_len++] = (char)c;
        }
        else
        {
            s_line_len = 0u;
            reply_err("toolong");
        }
    }
}

uint8_t Protocol_BootRequested(void)
{
    return s_boot_requested;
}

/* --------------------------------------------------------------------------
 * Bootloader entry.
 *
 * Sets a one-shot flag in the backup registers and resets. The board's own
 * bootloader (bootloader/, first 12 KB of flash) sees the flag and stays
 * resident on USB instead of starting this application (see bootflag.h).
 * The BOOT0 jumper (JP2) and the ROM bootloader remain the recovery path.
 * ----------------------------------------------------------------------- */
void Protocol_JumpToBootloader(void)
{
    Servo_DisableAll();

    /* Drop off the bus so the host sees a clean detach before the
       bootloader enumerates. */
    USB_Port_Set(DISABLE, DISABLE);
    Delay_Ms(20);

    BootFlag_RequestBootloader();    /* resets; does not return */
}
