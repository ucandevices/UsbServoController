/********************************************************************************
 * sense.c -- servo-rail current and voltage sensing. See sense.h.
 ********************************************************************************/

#include "sense.h"
#include "servo.h"

#define ADC_CH_ISENSE          ADC_Channel_5      /* PA5, U4 INA180A2 output   */
#define ADC_CH_VSENSE          ADC_Channel_4      /* PA4, R21/R22 divider tap  */

#define SENSE_ADC_FULL         4095u
#define SENSE_FULL_SCALE       13200u             /* see the header: mA and mV */

#define SENSE_AVG_SAMPLES      8u
#define SENSE_TARE_SAMPLES     32u

/* A trip needs this many consecutive out-of-range readings. At one reading per
   SENSE_TASK_PERIOD_MS that is 50 ms of genuine overload, which rejects servo
   start-up surges without meaningfully delaying a real fault. */
#define SENSE_TRIP_COUNT       5u
#define SENSE_TASK_PERIOD_MS   10u
#define SENSE_STARTUP_GRACE_MS 250u

static uint16_t s_i_offset;          /* INA180A2 zero-current output, in counts */
static uint32_t s_limit_ma = SENSE_DEFAULT_LIMIT_MA;
static uint32_t s_limit_mv;          /* 0 until AUTO classifies a supply     */
static bool     s_uv_auto = true;    /* cleared by any 'P' command           */
static uint8_t  s_uv_settle;         /* consecutive present readings, AUTO   */

static uint8_t  s_faults;
static bool     s_rail_present;
static uint32_t s_last_ma;
static uint32_t s_last_mv;

static uint8_t  s_oc_count;
static uint8_t  s_uv_count;
static uint32_t s_ms;

/* ------------------------------------------------------------------------- */

void Sense_Init(void)
{
    ADC_InitTypeDef adc = {0};
    GPIO_InitTypeDef gpio = {0};

    RCC_APB2PeriphClockCmd(RCC_APB2Periph_ADC1, ENABLE);
    /* ADC clock must stay <= 14 MHz: 96 MHz / 8 = 12 MHz. */
    RCC_ADCCLKConfig(RCC_PCLK2_Div8);

    /* PA4 = V_SENSE, PA5 = I_SENSE: permanently analog, never driven. */
    gpio.GPIO_Pin  = GPIO_Pin_4 | GPIO_Pin_5;
    gpio.GPIO_Mode = GPIO_Mode_AIN;
    GPIO_Init(GPIOA, &gpio);

    adc.ADC_Mode               = ADC_Mode_Independent;
    adc.ADC_ScanConvMode       = DISABLE;
    adc.ADC_ContinuousConvMode = DISABLE;
    adc.ADC_ExternalTrigConv   = ADC_ExternalTrigConv_None;
    adc.ADC_DataAlign          = ADC_DataAlign_Right;
    adc.ADC_NbrOfChannel       = 1;
    ADC_Init(ADC1, &adc);

    ADC_Cmd(ADC1, ENABLE);

    ADC_ResetCalibration(ADC1);
    while (ADC_GetResetCalibrationStatus(ADC1)) { }
    ADC_StartCalibration(ADC1);
    while (ADC_GetCalibrationStatus(ADC1)) { }
}

bool Sense_SampleAdc(uint8_t adc_channel, uint16_t *raw)
{
    uint32_t guard = 0;

    if (raw == NULL)
    {
        return false;
    }

    /* 239.5 cycles at 12 MHz is ~20 us of sampling. The voltage divider's
       Thevenin impedance is 5k1 || 15k3 = 3.8 kOhm, well inside what that
       sample time supports, and C18 supplies the sampling capacitor anyway. */
    ADC_RegularChannelConfig(ADC1, adc_channel, 1, ADC_SampleTime_239Cycles5);
    ADC_SoftwareStartConvCmd(ADC1, ENABLE);

    while (ADC_GetFlagStatus(ADC1, ADC_FLAG_EOC) == RESET)
    {
        if (++guard > 1000000u)
        {
            return false;
        }
    }

    *raw = ADC_GetConversionValue(ADC1);
    return true;
}

static bool sample_avg(uint8_t adc_channel, uint8_t n, uint16_t *out)
{
    uint32_t sum = 0;
    uint16_t raw;
    uint8_t  i;

    for (i = 0; i < n; i++)
    {
        if (!Sense_SampleAdc(adc_channel, &raw))
        {
            return false;
        }
        sum += raw;
    }

    *out = (uint16_t)(sum / n);
    return true;
}

static uint32_t raw_to_units(uint16_t raw)
{
    return ((uint32_t)raw * SENSE_FULL_SCALE) / SENSE_ADC_FULL;
}

/* ------------------------------------------------------------------------- */

bool Sense_ReadRaw(uint8_t idx, uint16_t *raw)
{
    if (raw == NULL || idx > SENSE_RAW_VOLTAGE)
    {
        return false;
    }
    return sample_avg(idx == SENSE_RAW_CURRENT ? ADC_CH_ISENSE : ADC_CH_VSENSE,
                      SENSE_AVG_SAMPLES, raw);
}

bool Sense_ReadCurrent_mA(uint32_t *ma)
{
    uint16_t raw;

    if (ma == NULL || !sample_avg(ADC_CH_ISENSE, SENSE_AVG_SAMPLES, &raw))
    {
        return false;
    }

    /* Unidirectional part: anything at or below the stored zero is no current,
       not negative current. */
    *ma = (raw <= s_i_offset) ? 0u : raw_to_units((uint16_t)(raw - s_i_offset));
    return true;
}

bool Sense_ReadVoltage_mV(uint32_t *mv)
{
    uint16_t raw;

    if (mv == NULL || !sample_avg(ADC_CH_VSENSE, SENSE_AVG_SAMPLES, &raw))
    {
        return false;
    }

    *mv = raw_to_units(raw);
    return true;
}

bool Sense_TareCurrent(uint16_t *offset_out)
{
    uint16_t raw;

    /* Only meaningful with nothing drawing current. */
    Servo_DisableAll();

    if (!sample_avg(ADC_CH_ISENSE, SENSE_TARE_SAMPLES, &raw))
    {
        return false;
    }

    s_i_offset = raw;
    if (offset_out != NULL)
    {
        *offset_out = raw;
    }
    return true;
}

uint16_t Sense_CurrentOffset(void) { return s_i_offset; }

void Sense_SetLimits(uint32_t ma, uint32_t mv)
{
    s_limit_ma = ma;
    s_limit_mv = mv;
    s_uv_auto  = false;
    s_oc_count = 0u;
    s_uv_count = 0u;
}

bool Sense_UndervoltageAuto(void) { return s_uv_auto; }

/* AUTO mode: pick an undervoltage limit from a settled supply voltage. See the
   SENSE_UV_* block in sense.h for the bands and why they sit where they do. */
static uint32_t uv_limit_for(uint32_t mv)
{
    if (mv >= SENSE_UV_3S_MIN_MV)
    {
        return SENSE_UV_3S_LIMIT_MV;
    }
    if (mv >= SENSE_UV_2S_MIN_MV && mv <= SENSE_UV_2S_MAX_MV)
    {
        return SENSE_UV_2S_LIMIT_MV;
    }
    return 0u;
}

void Sense_GetLimits(uint32_t *ma, uint32_t *mv)
{
    if (ma != NULL) { *ma = s_limit_ma; }
    if (mv != NULL) { *mv = s_limit_mv; }
}

uint8_t  Sense_Faults(void)         { return s_faults; }
bool     Sense_RailPresent(void)    { return s_rail_present; }
uint32_t Sense_LastCurrent_mA(void) { return s_last_ma; }
uint32_t Sense_LastVoltage_mV(void) { return s_last_mv; }

void Sense_ClearFaults(void)
{
    s_faults   = 0u;
    s_oc_count = 0u;
    s_uv_count = 0u;
}

/* ------------------------------------------------------------------------- */

void Sense_Task(void)
{
    uint32_t ma, mv;

    s_ms++;

    /* C15/C16 are 940 uF downstream of the shunt, so power-on inrush shows up
       as a large but brief current. Ignore everything until it has settled. */
    if (s_ms < SENSE_STARTUP_GRACE_MS)
    {
        return;
    }

    if ((s_ms % SENSE_TASK_PERIOD_MS) != 0u)
    {
        return;
    }

    if (Sense_ReadCurrent_mA(&ma))
    {
        s_last_ma = ma;

        if (s_limit_ma != 0u && ma > s_limit_ma)
        {
            if (s_oc_count < SENSE_TRIP_COUNT && ++s_oc_count >= SENSE_TRIP_COUNT)
            {
                /* Dropping the pulses de-energises every servo, so the current
                   goes away without any series element in the rail. This is
                   what stands in for the overcurrent protection the board has
                   no room or budget for. */
                s_faults |= SENSE_FAULT_OVERCURRENT;
                Servo_DisableAll();
            }
        }
        else
        {
            s_oc_count = 0u;
        }
    }

    if (Sense_ReadVoltage_mV(&mv))
    {
        s_last_mv = mv;
        s_rail_present = (mv >= SENSE_RAIL_PRESENT_MV);

        if (!s_rail_present)
        {
            /* No servo supply connected at all -- running from USB only. That
               is a normal configuration, not an undervoltage fault. In AUTO
               mode, forget the last supply so the next one is re-detected. */
            s_uv_count = 0u;
            if (s_uv_auto)
            {
                s_uv_settle = 0u;
                s_limit_mv  = 0u;
            }
        }
        else if (s_uv_auto && s_uv_settle < SENSE_UV_SETTLE_READS)
        {
            /* Wait for the supply to settle before classifying it, so a
               reading taken while C15/C16 are still charging cannot put a
               pack in the wrong band. Nothing trips meanwhile. */
            if (++s_uv_settle >= SENSE_UV_SETTLE_READS)
            {
                s_limit_mv = uv_limit_for(mv);
            }
        }
        else if (s_limit_mv != 0u && mv < s_limit_mv)
        {
            if (s_uv_count < SENSE_TRIP_COUNT && ++s_uv_count >= SENSE_TRIP_COUNT)
            {
                /* Protects a LiPo from being discharged past its cell floor
                   when JP1 is closed and the board is running off the pack. */
                s_faults |= SENSE_FAULT_UNDERVOLTAGE;
                Servo_DisableAll();
            }
        }
        else
        {
            s_uv_count = 0u;
        }
    }
}
