/********************************************************************************
 * script.c -- stored servo scripts that run without a PC. See script.h.
 ********************************************************************************/
#include <stdio.h>
#include <string.h>

#include "ch32v20x.h"
#include "script.h"
#include "servo.h"
#include "sense.h"
#include "flash_layout.h"

#define SCRIPT_MAGIC        0x31524353u     /* "SCR1" */
#define SCRIPT_IMAGE_SIZE   2048u           /* header + instructions, 8 flash pages */
#define ANALOG_POLL_MS      10u
#define INSTR_PER_TICK      32u             /* a tight loop cannot starve USB */

typedef struct
{
    uint32_t magic;
    uint16_t count;
    uint8_t  flags;
    uint8_t  reserved0;
    uint32_t crc;
    uint32_t reserved1;
} ScriptHeader_t;

typedef union
{
    struct
    {
        ScriptHeader_t hdr;
        ScriptInstr_t  code[SCRIPT_MAX_INSTR];
    } s;
    uint32_t words[SCRIPT_IMAGE_SIZE / 4u];
} ScriptImage_t;

_Static_assert(sizeof(ScriptHeader_t) == 16u, "header layout");
_Static_assert(sizeof(ScriptInstr_t) == 8u, "instruction layout");
_Static_assert(sizeof(ScriptImage_t) == SCRIPT_IMAGE_SIZE, "image layout");
_Static_assert(SCRIPT_IMAGE_SIZE <= SCRIPT_SIZE, "script area too small");

#define FLASH_IMAGE ((const ScriptImage_t *)SCRIPT_FLASH_ADDR)

typedef enum { WAIT_NONE = 0, WAIT_TIME, WAIT_SYNC, WAIT_INPUT } WaitKind_t;

typedef struct
{
    bool     active;
    uint16_t from;
    uint16_t to;
    uint16_t last;
    uint32_t t0;
    uint32_t dur;
} Ramp_t;

static ScriptImage_t s_stage;                        /* upload buffer, RAM */
static uint16_t      s_loop_left[SCRIPT_MAX_INSTR];
static Ramp_t        s_ramp[SERVO_CHANNELS];

static ScriptState_t s_state;
static uint16_t      s_pc;
static WaitKind_t    s_wait;
static uint32_t      s_wait_until;
static uint8_t       s_wait_ch;
static uint16_t      s_wait_thr;
static uint8_t       s_wait_cond;
static uint32_t      s_next_poll;

/* ms clock from TIM2, the servo timer: 2 MHz, wraps every 20 ms. Task runs
   every ~1 ms, far inside one wrap, so the difference is never ambiguous. */
static uint32_t s_now_ms;
static uint32_t s_tick_acc;
static uint16_t s_last_cnt;

/* --- helpers -------------------------------------------------------------- */

static uint32_t crc32(const uint8_t *p, uint32_t len)
{
    uint32_t crc = 0xFFFFFFFFu;
    int k;

    while (len--)
    {
        crc ^= *p++;
        for (k = 0; k < 8; k++)
        {
            crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
        }
    }
    return ~crc;
}

static bool flash_valid(void)
{
    const ScriptHeader_t *h = &FLASH_IMAGE->s.hdr;

    return h->magic == SCRIPT_MAGIC && h->count > 0u && h->count <= SCRIPT_MAX_INSTR &&
           h->crc == crc32((const uint8_t *)FLASH_IMAGE->s.code,
                           (uint32_t)h->count * sizeof(ScriptInstr_t));
}

static void clock_update(void)
{
    uint16_t cnt = (uint16_t)TIM2->CNT;
    uint32_t d = (cnt >= s_last_cnt) ? (uint32_t)(cnt - s_last_cnt)
                                     : (uint32_t)cnt + SERVO_FRAME_TICKS - s_last_cnt;
    s_last_cnt = cnt;
    s_tick_acc += d;
    while (s_tick_acc >= SERVO_TICKS_PER_US * 1000u)
    {
        s_tick_acc -= SERVO_TICKS_PER_US * 1000u;
        s_now_ms++;
    }
}

static bool input_holds(uint8_t ch, uint16_t thr, uint8_t cond)
{
    uint16_t v;

    if (!Servo_ReadAnalog(ch, &v))
    {
        return false;
    }
    return cond ? (v < thr) : (v > thr);
}

/* Validate one instruction on upload. Jump targets are checked at commit,
   once the final count is known. */
static bool instr_ok(const ScriptInstr_t *in)
{
    switch (in->op)
    {
        case SOP_END:
        case SOP_SYNC:
        case SOP_WAIT:
        case SOP_LOOP:
        case SOP_JUMP:
            return true;
        case SOP_MOVE:
            return in->ch < SERVO_CHANNELS &&
                   in->a >= SERVO_US_MIN && in->a <= SERVO_US_MAX;
        case SOP_OFF:
            return in->ch < SERVO_CHANNELS || in->ch == SCRIPT_CH_ALL;
        case SOP_WAITIN:
        case SOP_IFIN:
            return Servo_HasAnalog(in->ch) && in->a <= 4095u && in->b <= 1u;
        default:
            return false;
    }
}

/* --- ramps ------------------------------------------------------------------ */

static void ramp_start(uint8_t ch, uint16_t to, uint32_t ms)
{
    Ramp_t *r = &s_ramp[ch];
    uint16_t from;

    if (r->active)
    {
        from = r->last;
    }
    else
    {
        /* An off channel ramps from where it was last driven: an unpowered
           servo stays put, so that is the best guess of where it is. */
        from = Servo_LastPulseUs(ch);
    }

    if (ms == 0u || from == to)
    {
        r->active = false;
        r->last = to;
        Servo_SetPulseUs(ch, to);
        return;
    }
    r->active = true;
    r->from = from;
    r->to = to;
    r->last = from;
    r->t0 = s_now_ms;
    r->dur = ms;
    Servo_SetPulseUs(ch, from);
}

static void ramps_update(void)
{
    uint8_t ch;

    for (ch = 0; ch < SERVO_CHANNELS; ch++)
    {
        Ramp_t *r = &s_ramp[ch];
        uint32_t el;
        uint16_t pos;

        if (!r->active)
        {
            continue;
        }
        el = s_now_ms - r->t0;
        if (el >= r->dur)
        {
            pos = r->to;
            r->active = false;
        }
        else
        {
            pos = (uint16_t)((int32_t)r->from +
                             ((int32_t)r->to - (int32_t)r->from) * (int32_t)el / (int32_t)r->dur);
        }
        if (pos != r->last)
        {
            r->last = pos;
            Servo_SetPulseUs(ch, pos);
        }
    }
}

static bool ramps_busy(void)
{
    uint8_t ch;
    for (ch = 0; ch < SERVO_CHANNELS; ch++)
    {
        if (s_ramp[ch].active)
        {
            return true;
        }
    }
    return false;
}

static void ramps_cancel(void)
{
    memset(s_ramp, 0, sizeof s_ramp);
}

/* --- execution -------------------------------------------------------------- */

static bool script_start(void)
{
    if (!flash_valid())
    {
        return false;
    }
    ramps_cancel();
    memset(s_loop_left, 0, sizeof s_loop_left);
    s_pc = 0;
    s_wait = WAIT_NONE;
    s_state = SCRIPT_RUNNING;
    return true;
}

void Script_Stop(void)
{
    if (s_state == SCRIPT_RUNNING)
    {
        s_state = SCRIPT_IDLE;
    }
    ramps_cancel();
    s_wait = WAIT_NONE;
}

bool Script_Running(void)
{
    return s_state == SCRIPT_RUNNING;
}

void Script_Init(void)
{
    s_last_cnt = (uint16_t)TIM2->CNT;
    s_state = SCRIPT_IDLE;
    if (flash_valid() && (FLASH_IMAGE->s.hdr.flags & SCRIPT_FLAG_AUTORUN))
    {
        script_start();
    }
}

/* True while the current wait still holds. */
static bool still_waiting(void)
{
    switch (s_wait)
    {
        case WAIT_TIME:
            return (int32_t)(s_now_ms - s_wait_until) < 0;
        case WAIT_SYNC:
            return ramps_busy();
        case WAIT_INPUT:
            if ((int32_t)(s_now_ms - s_next_poll) < 0)
            {
                return true;
            }
            s_next_poll = s_now_ms + ANALOG_POLL_MS;
            return !input_holds(s_wait_ch, s_wait_thr, s_wait_cond);
        default:
            return false;
    }
}

static void step(const ScriptInstr_t *code, uint16_t count)
{
    uint32_t budget;

    for (budget = INSTR_PER_TICK; budget; budget--)
    {
        const ScriptInstr_t *in;

        if (s_wait != WAIT_NONE)
        {
            if (still_waiting())
            {
                return;
            }
            s_wait = WAIT_NONE;
        }
        if (s_pc >= count)
        {
            s_state = SCRIPT_DONE;
            return;
        }

        in = &code[s_pc];
        switch (in->op)
        {
            case SOP_END:
                s_state = SCRIPT_DONE;
                return;

            case SOP_MOVE:
                ramp_start(in->ch, in->a, in->b);
                s_pc++;
                break;

            case SOP_WAIT:
                s_wait_until = s_now_ms + in->b;
                s_wait = WAIT_TIME;
                s_pc++;
                break;

            case SOP_SYNC:
                s_wait = WAIT_SYNC;
                s_pc++;
                break;

            case SOP_OFF:
                if (in->ch == SCRIPT_CH_ALL)
                {
                    ramps_cancel();
                    Servo_DisableAll();
                }
                else
                {
                    s_ramp[in->ch].active = false;
                    Servo_SetEnabled(in->ch, false);
                }
                s_pc++;
                break;

            case SOP_LOOP:
                if (in->b == 0u)
                {
                    s_pc = in->a;                   /* forever */
                    break;
                }
                if (s_loop_left[s_pc] == 0u)
                {
                    s_loop_left[s_pc] = (uint16_t)in->b;
                }
                if (--s_loop_left[s_pc] > 0u)
                {
                    s_pc = in->a;
                }
                else
                {
                    s_pc++;                         /* done; re-arms next time */
                }
                break;

            case SOP_JUMP:
                s_pc = in->a;
                break;

            case SOP_WAITIN:
                s_wait_ch = in->ch;
                s_wait_thr = in->a;
                s_wait_cond = (uint8_t)in->b;
                s_next_poll = s_now_ms;
                s_wait = WAIT_INPUT;
                s_pc++;
                break;

            case SOP_IFIN:
                s_pc += input_holds(in->ch, in->a, (uint8_t)in->b) ? 1u : 2u;
                break;

            default:
                s_state = SCRIPT_DONE;              /* cannot happen: validated */
                return;
        }
    }
}

void Script_Task(void)
{
    clock_update();

    if (s_state != SCRIPT_RUNNING)
    {
        return;
    }
    if (Sense_Faults() != 0u)
    {
        /* Sense has already dropped every channel; do not drive them again. */
        ramps_cancel();
        s_state = SCRIPT_FAULTED;
        return;
    }
    ramps_update();
    step(FLASH_IMAGE->s.code, FLASH_IMAGE->s.hdr.count);
}

/* --- host commands ---------------------------------------------------------- */

static bool parse_u32(const char **p, uint32_t *out)
{
    const char *s = *p;
    uint32_t v = 0;
    uint8_t digits = 0;

    while (*s == ' ' || *s == '\t') { s++; }
    while (*s >= '0' && *s <= '9')
    {
        v = (v * 10u) + (uint32_t)(*s - '0');
        s++;
        if (++digits > 9u) { return false; }
    }
    if (digits == 0u) { return false; }
    *p = s;
    *out = v;
    return true;
}

static void commit(const char *p, char *out, uint32_t n)
{
    uint32_t count, flags, i;
    ScriptHeader_t *h = &s_stage.s.hdr;

    if (!parse_u32(&p, &count) || !parse_u32(&p, &flags))
    {
        snprintf(out, n, "ERR syntax\r\n");
        return;
    }
    if (count > SCRIPT_MAX_INSTR)
    {
        snprintf(out, n, "ERR count\r\n");
        return;
    }
    for (i = 0; i < count; i++)
    {
        const ScriptInstr_t *in = &s_stage.s.code[i];
        if ((in->op == SOP_LOOP || in->op == SOP_JUMP) && in->a >= count)
        {
            snprintf(out, n, "ERR target %lu\r\n", (unsigned long)i);
            return;
        }
    }

    Script_Stop();                  /* never run from flash while it is rewritten */
    h->magic = SCRIPT_MAGIC;
    h->count = (uint16_t)count;
    h->flags = (uint8_t)flags;
    h->reserved0 = 0u;
    h->reserved1 = 0u;
    h->crc = crc32((const uint8_t *)s_stage.s.code, count * sizeof(ScriptInstr_t));

    if (FLASH_ROM_ERASE(SCRIPT_FLASH_ADDR, SCRIPT_SIZE) != FLASH_COMPLETE ||
        FLASH_ROM_WRITE(SCRIPT_FLASH_ADDR, s_stage.words, SCRIPT_IMAGE_SIZE) != FLASH_COMPLETE)
    {
        snprintf(out, n, "ERR flash\r\n");
        return;
    }
    if (memcmp((const void *)SCRIPT_FLASH_ADDR, &s_stage, SCRIPT_IMAGE_SIZE) != 0)
    {
        snprintf(out, n, "ERR verify\r\n");
        return;
    }
    snprintf(out, n, "OK %lu\r\n", (unsigned long)h->crc);
}

void Script_Command(const char *p, char *out, uint32_t n)
{
    char sub;
    uint32_t idx, op, ch, a, b;

    while (*p == ' ') { p++; }
    sub = *p;
    if (sub >= 'a' && sub <= 'z') { sub = (char)(sub - 'a' + 'A'); }
    if (sub != '\0') { p++; }

    switch (sub)
    {
        case 'C':                                   /* clear the upload buffer */
            memset(&s_stage, 0, sizeof s_stage);
            snprintf(out, n, "OK\r\n");
            break;

        case 'A':                                   /* A idx op ch a b */
            if (!parse_u32(&p, &idx) || !parse_u32(&p, &op) || !parse_u32(&p, &ch) ||
                !parse_u32(&p, &a) || !parse_u32(&p, &b))
            {
                snprintf(out, n, "ERR syntax\r\n");
                break;
            }
            if (idx >= SCRIPT_MAX_INSTR || op >= SOP_COUNT || ch > 255u || a > 65535u)
            {
                snprintf(out, n, "ERR range\r\n");
                break;
            }
            {
                ScriptInstr_t in = { (uint8_t)op, (uint8_t)ch, (uint16_t)a, b };
                if (!instr_ok(&in))
                {
                    snprintf(out, n, "ERR instr\r\n");
                    break;
                }
                s_stage.s.code[idx] = in;
            }
            snprintf(out, n, "OK\r\n");
            break;

        case 'S':                                   /* S count flags: save to flash */
            commit(p, out, n);
            break;

        case 'I':                                   /* OK valid count flags state pc */
        {
            const ScriptHeader_t *h = &FLASH_IMAGE->s.hdr;
            bool valid = flash_valid();
            snprintf(out, n, "OK %u %u %u %u %u\r\n", valid ? 1u : 0u,
                     valid ? h->count : 0u, valid ? h->flags : 0u,
                     (unsigned)s_state, (unsigned)s_pc);
            break;
        }

        case 'G':                                   /* G idx: read back from flash */
        {
            const ScriptInstr_t *in;
            if (!parse_u32(&p, &idx) || idx >= SCRIPT_MAX_INSTR)
            {
                snprintf(out, n, "ERR syntax\r\n");
                break;
            }
            in = &FLASH_IMAGE->s.code[idx];
            snprintf(out, n, "OK %u %u %u %lu\r\n", in->op, in->ch, in->a,
                     (unsigned long)in->b);
            break;
        }

        case 'R':                                   /* run the stored script */
            snprintf(out, n, script_start() ? "OK\r\n" : "ERR noscript\r\n");
            break;

        case 'X':                                   /* stop it; servos hold */
            Script_Stop();
            snprintf(out, n, "OK\r\n");
            break;

        default:
            snprintf(out, n, "ERR unknown\r\n");
            break;
    }
}
