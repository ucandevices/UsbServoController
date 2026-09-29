/********************************************************************************
 * servo.c -- 12-channel servo driver, CH32V203C8T6 port. See servo.h.
 ********************************************************************************/

#include "servo.h"
#include "sense.h"

#define SERVO_PSC   ((SystemCoreClock / SERVO_TIMER_HZ) - 1u)   /* 96M/2M-1 = 47 */
#define SERVO_ARR   (SERVO_FRAME_TICKS - 1u)                    /* 39999        */

typedef struct
{
    TIM_TypeDef  *tim;
    uint8_t       channel;      /* 1..4                                        */
    GPIO_TypeDef *port;
    uint16_t      pin;
    int16_t       adc_channel;  /* ADC_Channel_x, or -1 if not analog-capable  */
} ServoChannel_t;

static const ServoChannel_t s_map[SERVO_CHANNELS] =
{
    /* ch0  */ { TIM2, 1, GPIOA, GPIO_Pin_0,  ADC_Channel_0 },
    /* ch1  */ { TIM2, 2, GPIOA, GPIO_Pin_1,  ADC_Channel_1 },
    /* ch2  */ { TIM2, 3, GPIOA, GPIO_Pin_2,  ADC_Channel_2 },
    /* ch3  */ { TIM2, 4, GPIOA, GPIO_Pin_3,  ADC_Channel_3 },
    /* ch4  */ { TIM3, 1, GPIOA, GPIO_Pin_6,  ADC_Channel_6 },
    /* ch5  */ { TIM3, 2, GPIOA, GPIO_Pin_7,  ADC_Channel_7 },
    /* ch6  */ { TIM3, 3, GPIOB, GPIO_Pin_0,  ADC_Channel_8 },
    /* ch7  */ { TIM3, 4, GPIOB, GPIO_Pin_1,  ADC_Channel_9 },
    /* ch8  */ { TIM1, 1, GPIOA, GPIO_Pin_8,  -1 },
    /* ch9  */ { TIM1, 2, GPIOA, GPIO_Pin_9,  -1 },
    /* ch10 */ { TIM1, 3, GPIOA, GPIO_Pin_10, -1 },
    /* ch11 */ { TIM4, 3, GPIOB, GPIO_Pin_8,  -1 },
};

static uint16_t s_pulse_us[SERVO_CHANNELS];
static bool     s_enabled[SERVO_CHANNELS];

/* ------------------------------------------------------------------------- */

static inline uint16_t us_to_ticks(uint16_t us)
{
    return (uint16_t)(us * SERVO_TICKS_PER_US);
}

static void write_ccr(const ServoChannel_t *c, uint16_t ticks)
{
    switch (c->channel)
    {
        case 1: TIM_SetCompare1(c->tim, ticks); break;
        case 2: TIM_SetCompare2(c->tim, ticks); break;
        case 3: TIM_SetCompare3(c->tim, ticks); break;
        default: TIM_SetCompare4(c->tim, ticks); break;
    }
}

static void timer_base_init(TIM_TypeDef *tim)
{
    TIM_TimeBaseInitTypeDef tb = {0};

    tb.TIM_Prescaler         = (uint16_t)SERVO_PSC;
    tb.TIM_CounterMode       = TIM_CounterMode_Up;
    tb.TIM_Period            = (uint16_t)SERVO_ARR;
    tb.TIM_ClockDivision     = TIM_CKD_DIV1;
    tb.TIM_RepetitionCounter = 0;
    TIM_TimeBaseInit(tim, &tb);
    TIM_ARRPreloadConfig(tim, ENABLE);
}

static void timer_channel_init(const ServoChannel_t *c)
{
    TIM_OCInitTypeDef oc = {0};

    oc.TIM_OCMode      = TIM_OCMode_PWM1;
    oc.TIM_OutputState = TIM_OutputState_Enable;
    oc.TIM_Pulse       = 0;                 /* idle low until commanded */
    oc.TIM_OCPolarity  = TIM_OCPolarity_High;

    switch (c->channel)
    {
        case 1: TIM_OC1Init(c->tim, &oc); TIM_OC1PreloadConfig(c->tim, TIM_OCPreload_Enable); break;
        case 2: TIM_OC2Init(c->tim, &oc); TIM_OC2PreloadConfig(c->tim, TIM_OCPreload_Enable); break;
        case 3: TIM_OC3Init(c->tim, &oc); TIM_OC3PreloadConfig(c->tim, TIM_OCPreload_Enable); break;
        default: TIM_OC4Init(c->tim, &oc); TIM_OC4PreloadConfig(c->tim, TIM_OCPreload_Enable); break;
    }
}

void Servo_Init(void)
{
    GPIO_InitTypeDef gpio = {0};
    uint8_t ch;

    RCC_APB2PeriphClockCmd(RCC_APB2Periph_GPIOA | RCC_APB2Periph_GPIOB |
                           RCC_APB2Periph_TIM1  | RCC_APB2Periph_AFIO, ENABLE);
    RCC_APB1PeriphClockCmd(RCC_APB1Periph_TIM2 | RCC_APB1Periph_TIM3 |
                           RCC_APB1Periph_TIM4, ENABLE);

    /* All 12 servo pins: alternate-function push-pull. */
    gpio.GPIO_Mode  = GPIO_Mode_AF_PP;
    gpio.GPIO_Speed = GPIO_Speed_50MHz;
    for (ch = 0; ch < SERVO_CHANNELS; ch++)
    {
        gpio.GPIO_Pin = s_map[ch].pin;
        GPIO_Init(s_map[ch].port, &gpio);
    }

    timer_base_init(TIM1);
    timer_base_init(TIM2);
    timer_base_init(TIM3);
    timer_base_init(TIM4);

    for (ch = 0; ch < SERVO_CHANNELS; ch++)
    {
        s_pulse_us[ch] = SERVO_US_NEUTRAL;
        s_enabled[ch]  = false;
        timer_channel_init(&s_map[ch]);
        write_ccr(&s_map[ch], 0);
    }

    TIM_Cmd(TIM1, ENABLE);
    TIM_Cmd(TIM2, ENABLE);
    TIM_Cmd(TIM3, ENABLE);
    TIM_Cmd(TIM4, ENABLE);

    /* TIM1 is an advanced timer: outputs stay high-Z until MOE is set. */
    TIM_CtrlPWMOutputs(TIM1, ENABLE);
}

bool Servo_SetPulseUs(uint8_t ch, uint16_t us)
{
    if (ch >= SERVO_CHANNELS)
    {
        return false;
    }
    if (us < SERVO_US_MIN) { us = SERVO_US_MIN; }
    if (us > SERVO_US_MAX) { us = SERVO_US_MAX; }

    s_pulse_us[ch] = us;
    s_enabled[ch]  = true;
    write_ccr(&s_map[ch], us_to_ticks(us));
    return true;
}

uint16_t Servo_GetPulseUs(uint8_t ch)
{
    if (ch >= SERVO_CHANNELS || !s_enabled[ch])
    {
        return 0u;
    }
    return s_pulse_us[ch];
}

uint16_t Servo_LastPulseUs(uint8_t ch)
{
    return (ch < SERVO_CHANNELS) ? s_pulse_us[ch] : 0u;
}

bool Servo_SetEnabled(uint8_t ch, bool enabled)
{
    if (ch >= SERVO_CHANNELS)
    {
        return false;
    }
    s_enabled[ch] = enabled;
    write_ccr(&s_map[ch], enabled ? us_to_ticks(s_pulse_us[ch]) : 0u);
    return true;
}

bool Servo_IsEnabled(uint8_t ch)
{
    return (ch < SERVO_CHANNELS) && s_enabled[ch];
}

void Servo_DisableAll(void)
{
    uint8_t ch;
    for (ch = 0; ch < SERVO_CHANNELS; ch++)
    {
        Servo_SetEnabled(ch, false);
    }
}

bool Servo_HasAnalog(uint8_t ch)
{
    return (ch < SERVO_CHANNELS) && (s_map[ch].adc_channel >= 0);
}

/* --------------------------------------------------------------------------
 * Analog sampling. ADC1 itself belongs to sense.c, which owns the two
 * permanently-analog pins (PA4/PA5) and initialises the peripheral; this file
 * borrows it through Sense_SampleAdc() for the dual-purpose servo pins. Both
 * callers run from the main loop, so the ADC needs no locking.
 * ----------------------------------------------------------------------- */

bool Servo_ReadAnalog(uint8_t ch, uint16_t *value)
{
    GPIO_InitTypeDef gpio = {0};
    const ServoChannel_t *c;
    bool was_enabled, ok;

    if (!Servo_HasAnalog(ch) || value == NULL)
    {
        return false;
    }

    c = &s_map[ch];
    was_enabled = s_enabled[ch];

    /* Park the output low so the pin is not driven while we read it. */
    write_ccr(c, 0);

    gpio.GPIO_Pin  = c->pin;
    gpio.GPIO_Mode = GPIO_Mode_AIN;
    GPIO_Init(c->port, &gpio);

    ok = Sense_SampleAdc((uint8_t)c->adc_channel, value);

    /* Restore PWM alternate function. */
    gpio.GPIO_Pin   = c->pin;
    gpio.GPIO_Mode  = GPIO_Mode_AF_PP;
    gpio.GPIO_Speed = GPIO_Speed_50MHz;
    GPIO_Init(c->port, &gpio);

    if (was_enabled)
    {
        write_ccr(c, us_to_ticks(s_pulse_us[ch]));
    }

    return ok;
}
