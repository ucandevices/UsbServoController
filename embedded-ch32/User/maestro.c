/********************************************************************************
 * maestro.c -- Pololu Maestro serial protocols (Compact, Pololu, Mini SSC).
 * See maestro.h for the command set.
 *
 * Motion follows the Maestro's rules: every 10 ms a moving channel's velocity
 * grows by acceleration/8 (the unit is per 80 ms), is capped at the speed
 * limit, and shrinks again once the remaining distance is what it takes to
 * stop. Position is kept in 1/8 quarter-microseconds so slow speeds still move.
 ********************************************************************************/

#include "maestro.h"
#include "servo.h"
#include "sense.h"
#include "script.h"

#include <string.h>

#define TICK_MS        10u
#define MAX_CATCHUP    8u             /* ticks to replay after a long stall     */
#define QUS_PER_US     4u
#define QUS_PER_TICK   (QUS_PER_US / SERVO_TICKS_PER_US)   /* 2: a tick is 0.5 us */
#define FIX            8u             /* position/velocity fraction, 1/8 qus    */

#define MINI_SSC_NEUTRAL_QUS  6000
#define MINI_SSC_RANGE_QUS    1905    /* Maestro default range, 476.25 us       */

typedef struct
{
    bool     moving;
    uint16_t speed;                   /* qus per 10 ms, 0 = unlimited           */
    uint16_t accel;                   /* qus per 10 ms per 80 ms, 0 = unlimited */
    uint32_t pos;                     /* qus * FIX                              */
    uint32_t target;                  /* qus * FIX                              */
    uint32_t vel;                     /* qus * FIX per tick                     */
    uint16_t last_ticks;              /* timer ticks last written               */
} Motion_t;

static Motion_t s_mo[SERVO_CHANNELS];
static uint16_t s_errors;
static uint32_t s_last_ms;
static bool     s_clock_started;

/* parser */
typedef enum { P_IDLE = 0, P_POLOLU_DEV, P_POLOLU_CMD, P_DATA, P_MINI_SERVO, P_MINI_TARGET } PState_t;

static PState_t s_ps;
static uint8_t  s_cmd;
static uint8_t  s_buf[2u + 2u * SERVO_CHANNELS];
static uint8_t  s_len;
static uint8_t  s_need;
static bool     s_ignore;             /* Pololu command for another device      */

/* --- motion ------------------------------------------------------------------ */

static uint16_t qus_to_ticks(uint32_t qus)
{
    return (uint16_t)((qus + QUS_PER_TICK / 2u) / QUS_PER_TICK);
}

static void set_target(uint8_t ch, uint16_t qus)
{
    Motion_t *m = &s_mo[ch];
    uint32_t tgt;
    uint16_t now_ticks, min_us, max_us;

    if (qus == 0u)
    {
        m->moving = false;
        Servo_SetEnabled(ch, false);
        return;
    }
    /* Never re-energise a servo while a rail fault is latched -- the same
       interlock the ASCII 'S' and 'E' commands honour. */
    if (Sense_Faults() != 0u)
    {
        return;
    }
    /* Clamped to the channel's travel limits, as a Maestro clamps to its
       channel min/max settings. */
    Servo_GetLimitsUs(ch, &min_us, &max_us);
    if (qus < min_us * QUS_PER_US) { qus = (uint16_t)(min_us * QUS_PER_US); }
    if (qus > max_us * QUS_PER_US) { qus = (uint16_t)(max_us * QUS_PER_US); }
    tgt = (uint32_t)qus * FIX;

    now_ticks = Servo_GetPulseTicks(ch);
    if (now_ticks == 0u || (m->speed == 0u && m->accel == 0u))
    {
        /* An off channel has no known position, so like a Maestro it starts
           at its target; with no limits the move is immediate anyway. */
        m->moving = false;
        m->pos = tgt;
        m->target = tgt;
        m->last_ticks = qus_to_ticks(qus);
        Servo_SetPulseTicks(ch, m->last_ticks);
        return;
    }
    if (!m->moving)
    {
        m->pos = (uint32_t)now_ticks * QUS_PER_TICK * FIX;
        m->vel = 0u;
        m->last_ticks = now_ticks;
    }
    else if ((tgt > m->pos) != (m->target > m->pos))
    {
        m->vel = 0u;                  /* reversing: start again from rest       */
    }
    m->target = tgt;
    m->moving = (m->pos != tgt);
}

static void step(uint8_t ch)
{
    Motion_t *m = &s_mo[ch];
    uint32_t remaining, vmax;
    uint16_t ticks;

    if (!m->moving)
    {
        return;
    }
    if (!Servo_IsEnabled(ch))
    {
        m->moving = false;            /* stopped by X, a rail fault or 'E'      */
        return;
    }

    remaining = (m->target > m->pos) ? m->target - m->pos : m->pos - m->target;
    vmax = (m->speed != 0u) ? (uint32_t)m->speed * FIX : 0xFFFFFFFFu;

    if (m->accel == 0u)
    {
        m->vel = vmax;
    }
    else
    {
        /* Distance needed to stop from vel, decelerating by accel per tick:
           vel^2 / (2 * accel), all in FIX units. */
        uint64_t stop = ((uint64_t)m->vel * m->vel) / (2u * (uint64_t)m->accel);

        if (stop >= remaining)
        {
            m->vel = (m->vel > m->accel) ? m->vel - m->accel : m->accel;
        }
        else
        {
            m->vel += m->accel;
            if (m->vel > vmax) { m->vel = vmax; }
        }
    }

    if (m->vel >= remaining)
    {
        m->pos = m->target;
        m->vel = 0u;
        m->moving = false;
    }
    else if (m->target > m->pos)
    {
        m->pos += m->vel;
    }
    else
    {
        m->pos -= m->vel;
    }

    ticks = qus_to_ticks(m->pos / FIX);
    if (ticks != m->last_ticks)
    {
        m->last_ticks = ticks;
        Servo_SetPulseTicks(ch, ticks);
    }
}

void Maestro_Task(void)
{
    uint32_t now = Servo_Millis();
    uint8_t  n = 0u, ch;

    if (!s_clock_started)
    {
        s_clock_started = true;
        s_last_ms = now;
    }
    while ((now - s_last_ms) >= TICK_MS)
    {
        s_last_ms += TICK_MS;
        if (++n > MAX_CATCHUP)
        {
            s_last_ms = now;          /* a long stall: don't jump, just resume  */
            break;
        }
        for (ch = 0; ch < SERVO_CHANNELS; ch++)
        {
            step(ch);
        }
    }
}

void Maestro_Cancel(uint8_t ch)
{
    if (ch < SERVO_CHANNELS)
    {
        s_mo[ch].moving = false;
    }
}

uint16_t Maestro_GetSpeed(uint8_t ch)            { return (ch < SERVO_CHANNELS) ? s_mo[ch].speed : 0u; }
uint16_t Maestro_GetAccel(uint8_t ch)            { return (ch < SERVO_CHANNELS) ? s_mo[ch].accel : 0u; }
void     Maestro_SetSpeed(uint8_t ch, uint16_t v) { if (ch < SERVO_CHANNELS) { s_mo[ch].speed = v; } }
void     Maestro_SetAccel(uint8_t ch, uint16_t v) { if (ch < SERVO_CHANNELS) { s_mo[ch].accel = (uint16_t)(v & 0xFFu); } }

void Maestro_CancelAll(void)
{
    uint8_t ch;
    for (ch = 0; ch < SERVO_CHANNELS; ch++)
    {
        s_mo[ch].moving = false;
    }
}

static uint16_t get_position(uint8_t ch)
{
    const Motion_t *m = &s_mo[ch];
    uint16_t ticks = Servo_GetPulseTicks(ch);

    if (m->moving)
    {
        return (uint16_t)(m->pos / FIX);
    }
    /* Arrived: report the exact quarter-us target, as a Maestro does -- clients
       compare it with what they sent -- unless something else (ASCII, the
       script) has since moved the channel. Output itself is 0.5 us steps. */
    if (ticks != 0u && ticks == qus_to_ticks(m->target / FIX))
    {
        return (uint16_t)(m->target / FIX);
    }
    return (uint16_t)(ticks * QUS_PER_TICK);
}

/* --- commands ---------------------------------------------------------------- */

/* Data bytes a compact command takes; 0xFF = unknown command. Set Multiple
   Targets is variable and handled by the caller. */
static uint8_t data_len(uint8_t cmd)
{
    switch (cmd)
    {
        case 0x84: case 0x87: case 0x89: case 0xA8: return 3u;
        case 0x8A:                                   return 4u;
        case 0x90: case 0xA7:                        return 1u;
        case 0x93: case 0xA1: case 0xA2:
        case 0xA4: case 0xAE:                        return 0u;
        case 0x9F:                                   return 2u;   /* count, first */
        default:                                     return 0xFFu;
    }
}

static uint16_t u14(const uint8_t *p)
{
    return (uint16_t)(p[0] | ((uint16_t)p[1] << 7));
}

static void execute(uint8_t cmd, const uint8_t *d, MaestroSend_t send)
{
    uint8_t out[2];
    uint8_t ch, i;

    switch (cmd)
    {
        case 0x84:                                        /* Set Target          */
            if (d[0] < SERVO_CHANNELS) { set_target(d[0], u14(&d[1])); }
            break;

        case 0x9F:                                        /* Set Multiple Targets */
            for (i = 0; i < d[0]; i++)
            {
                ch = (uint8_t)(d[1] + i);
                if (ch < SERVO_CHANNELS) { set_target(ch, u14(&d[2u + 2u * i])); }
            }
            break;

        case 0x87:                                        /* Set Speed           */
            Maestro_SetSpeed(d[0], u14(&d[1]));
            break;

        case 0x89:                                        /* Set Acceleration    */
            Maestro_SetAccel(d[0], u14(&d[1]));
            break;

        case 0x90:                                        /* Get Position        */
        {
            uint16_t q = (d[0] < SERVO_CHANNELS) ? get_position(d[0]) : 0u;
            out[0] = (uint8_t)(q & 0xFFu);
            out[1] = (uint8_t)(q >> 8);
            send(out, 2u);
            break;
        }

        case 0x93:                                        /* Get Moving State    */
            out[0] = 0u;
            for (ch = 0; ch < SERVO_CHANNELS; ch++)
            {
                if (s_mo[ch].moving && Servo_IsEnabled(ch)) { out[0] = 1u; }
            }
            send(out, 1u);
            break;

        case 0xA1:                                        /* Get Errors          */
            out[0] = (uint8_t)(s_errors & 0xFFu);
            out[1] = (uint8_t)(s_errors >> 8);
            s_errors = 0u;
            send(out, 2u);
            break;

        case 0xA2:                                        /* Go Home: all off    */
            Script_Stop();
            Maestro_CancelAll();
            Servo_DisableAll();
            break;

        case 0xA4:                                        /* Stop Script         */
            Script_Stop();
            break;

        case 0xAE:                                        /* Get Script Status   */
            out[0] = Script_Running() ? 0u : 1u;
            send(out, 1u);
            break;

        default:                                          /* Set PWM, Restart Script */
            break;
    }
}

static void start_compact(uint8_t cmd)
{
    s_cmd = cmd;
    s_len = 0u;
    s_need = data_len(cmd);
    if (s_need == 0xFFu)
    {
        s_errors |= MAESTRO_ERR_PROTOCOL;
        s_ps = P_IDLE;
        return;
    }
    s_ps = P_DATA;
}

static void finish_if_complete(MaestroSend_t send)
{
    if (s_len < s_need)
    {
        return;
    }
    if (s_cmd == 0x9F && s_need == 2u)
    {
        /* Now the count is known: 2 bytes per target follow. */
        if (s_buf[0] == 0u || s_buf[0] > SERVO_CHANNELS)
        {
            s_errors |= MAESTRO_ERR_PROTOCOL;
            s_ps = P_IDLE;
            return;
        }
        s_need = (uint8_t)(2u + 2u * s_buf[0]);
        return;
    }
    if (!s_ignore)
    {
        execute(s_cmd, s_buf, send);
    }
    s_ps = P_IDLE;
}

bool Maestro_Feed(uint8_t c, MaestroSend_t send)
{
    switch (s_ps)
    {
        case P_MINI_SERVO:                 /* Mini SSC bytes are full 8-bit     */
            s_buf[0] = c;
            s_ps = P_MINI_TARGET;
            return true;

        case P_MINI_TARGET:
            s_ps = P_IDLE;
            if (s_buf[0] < SERVO_CHANNELS && c <= 254u)
            {
                set_target(s_buf[0], (uint16_t)(MINI_SSC_NEUTRAL_QUS +
                           ((int32_t)c - 127) * MINI_SSC_RANGE_QUS / 127));
            }
            return true;

        case P_POLOLU_DEV:
            if (c & 0x80u) { break; }      /* new command: fall through below   */
            s_ignore = (c != MAESTRO_DEVICE_NUMBER);
            s_ps = P_POLOLU_CMD;
            return true;

        case P_POLOLU_CMD:
            if (c & 0x80u) { break; }
            start_compact((uint8_t)(c | 0x80u));
            if (s_ps == P_DATA) { finish_if_complete(send); }
            return true;

        case P_DATA:
            if (c & 0x80u) { break; }
            s_buf[s_len++] = c;
            finish_if_complete(send);
            return true;

        default:
            break;
    }

    /* Idle, or a command byte arrived where data was expected. */
    if (s_ps != P_IDLE)
    {
        s_errors |= MAESTRO_ERR_PROTOCOL;
        s_ps = P_IDLE;
    }
    if (!(c & 0x80u))
    {
        return false;                      /* ASCII: the line protocol's        */
    }
    s_ignore = false;
    if (c == 0xAAu)
    {
        s_ps = P_POLOLU_DEV;
    }
    else if (c == 0xFFu)
    {
        s_ps = P_MINI_SERVO;
    }
    else
    {
        start_compact(c);
        if (s_ps == P_DATA) { finish_if_complete(send); }
    }
    return true;
}
