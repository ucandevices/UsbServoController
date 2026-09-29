/* Host test for User/sense.c (run: make hosttest), compiled unmodified against a
   stubbed ADC. Each scenario runs in a fresh process (sense.c keeps static
   state), selected by argv[1]. One Sense_Task() call = one main-loop tick. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "sense.h"
#include "servo.h"

ADC_TypeDef  hosttest_adc1;
GPIO_TypeDef hosttest_gpioa;

static uint16_t g_raw[16];
static uint8_t  g_cur;
static int      g_disable_all_calls;

void RCC_APB2PeriphClockCmd(uint32_t p, FunctionalState s) { (void)p; (void)s; }
void RCC_ADCCLKConfig(uint32_t d) { (void)d; }
void GPIO_Init(GPIO_TypeDef *g, GPIO_InitTypeDef *i) { (void)g; (void)i; }
void ADC_Init(ADC_TypeDef *a, ADC_InitTypeDef *i) { (void)a; (void)i; }
void ADC_Cmd(ADC_TypeDef *a, FunctionalState s) { (void)a; (void)s; }
void ADC_ResetCalibration(ADC_TypeDef *a) { (void)a; }
FlagStatus ADC_GetResetCalibrationStatus(ADC_TypeDef *a) { (void)a; return RESET; }
void ADC_StartCalibration(ADC_TypeDef *a) { (void)a; }
FlagStatus ADC_GetCalibrationStatus(ADC_TypeDef *a) { (void)a; return RESET; }
void ADC_RegularChannelConfig(ADC_TypeDef *a, uint8_t ch, uint8_t r, uint8_t st)
{ (void)a; (void)r; (void)st; g_cur = ch; }
void ADC_SoftwareStartConvCmd(ADC_TypeDef *a, FunctionalState s) { (void)a; (void)s; }
FlagStatus ADC_GetFlagStatus(ADC_TypeDef *a, uint8_t f) { (void)a; (void)f; return SET; }
uint16_t ADC_GetConversionValue(ADC_TypeDef *a) { (void)a; return g_raw[g_cur]; }

/* servo.h surface used by sense.c */
void Servo_DisableAll(void) { g_disable_all_calls++; }

static void rail_mv(uint32_t mv)  { g_raw[ADC_Channel_4] = (uint16_t)((mv * 4095u + 6600u) / 13200u); }
static void rail_ma(uint32_t ma)  { g_raw[ADC_Channel_5] = (uint16_t)((ma * 4095u + 6600u) / 13200u); }
static void ticks(int n)          { while (n--) Sense_Task(); }

static int failures;
#define CHECK(cond, ...) do { if (cond) { printf("  ok    "); } else { printf("  FAIL  "); failures++; } \
                              printf(__VA_ARGS__); printf("\n"); } while (0)

static uint32_t limit_mv(void) { uint32_t ma, mv; Sense_GetLimits(&ma, &mv); return mv; }

/* Power up with a steady supply and let everything settle (grace + settle). */
static void power_up(uint32_t mv) { Sense_Init(); rail_ma(10); rail_mv(mv); ticks(1000); }

static void steady(const char *name, uint32_t mv, uint32_t want_limit)
{
    power_up(mv);
    printf("%s (%lu mV)\n", name, (unsigned long)mv);
    CHECK(Sense_Faults() == 0, "no fault latched (faults=%u)", Sense_Faults());
    CHECK(limit_mv() == want_limit, "undervoltage limit %lu, expected %lu",
          (unsigned long)limit_mv(), (unsigned long)want_limit);
    CHECK(Sense_UndervoltageAuto(), "still in AUTO mode");
    CHECK(g_disable_all_calls == 0, "servos never force-disabled");
}

int main(int argc, char **argv)
{
    int s = (argc > 1) ? atoi(argv[1]) : 0;

    switch (s)
    {
    /* --- the bug being fixed: common supplies must not fault ----------- */
    case 1: steady("5 V BEC",            5000,  0);    break;
    case 2: steady("6 V BEC",            6000,  0);    break;
    case 3: steady("6 V BEC, +3% high",  6250,  0);    break;
    case 4: steady("4x NiMH charged",    5600,  0);    break;
    case 5: steady("USB only, no supply",   0,  0);    break;

    /* --- LiPo detection ------------------------------------------------ */
    case 6: steady("2S LiPo charged",    8400, 6400);  break;
    case 7: steady("2S LiPo nominal",    7400, 6400);  break;
    case 8: steady("2S HV-LiPo charged", 8700, 6400);  break;
    case 9: steady("3S LiPo nominal",   11100, 9600);  break;
    case 10: steady("3S LiPo charged",  12600, 9600);  break;

    /* --- band edges, with ~3 mV/count quantisation margin -------------- */
    case 11: steady("just below 2S band", 6550,  0);   break;
    case 12: steady("just inside 2S band", 6650, 6400); break;
    case 13: steady("top of 2S band",     8750, 6400); break;
    case 14: steady("gap between 2S/3S",  9000,  0);   break;
    case 15: steady("just below 3S band", 9850,  0);   break;
    case 16: steady("just inside 3S band", 9950, 9600); break;

    case 20: /* 2S pack runs flat -> must trip and disable servos */
        power_up(7600);
        printf("2S pack discharging to 6.2 V\n");
        CHECK(limit_mv() == 6400, "detected as 2S (limit %lu)", (unsigned long)limit_mv());
        rail_mv(6200);
        ticks(30);                   /* 3 task periods: below the 5-reading trip */
        CHECK(Sense_Faults() == 0, "no trip after 3 low readings (debounce)");
        ticks(40);
        CHECK(Sense_Faults() & SENSE_FAULT_UNDERVOLTAGE, "undervoltage fault latched");
        CHECK(g_disable_all_calls >= 1, "Servo_DisableAll() called (%d)", g_disable_all_calls);
        break;

    case 21: /* supply ramps up slowly: first readings must not decide */
        Sense_Init(); rail_ma(10);
        printf("2S pack connected while C15/C16 charge\n");
        rail_mv(0);    ticks(300);   /* past the start-up grace, USB only   */
        rail_mv(3000); ticks(30);    /* present but still charging          */
        rail_mv(8200); ticks(200);
        CHECK(limit_mv() == 6400, "classified on the settled voltage (limit %lu)", (unsigned long)limit_mv());
        CHECK(Sense_Faults() == 0, "no fault during the ramp");
        break;

    case 22: /* pack swap: 2S out, 5 V BEC in -> must re-detect, not trip */
        power_up(8000);
        printf("swap 2S LiPo for a 5 V BEC\n");
        CHECK(limit_mv() == 6400, "2S detected first");
        rail_mv(0);    ticks(100);
        CHECK(limit_mv() == 0, "limit forgotten when the supply is removed");
        rail_mv(5000); ticks(500);
        CHECK(limit_mv() == 0, "5 V BEC re-detected: no cutoff");
        CHECK(Sense_Faults() == 0, "no fault after the swap");
        break;

    case 23: /* manual limits win, and stay across a supply change */
        power_up(5000);
        printf("manual P 5000 4500, then the supply sags and is swapped\n");
        Sense_SetLimits(5000, 4500);
        CHECK(!Sense_UndervoltageAuto(), "AUTO mode left after P");
        CHECK(limit_mv() == 4500, "manual limit in force");
        rail_mv(4300); ticks(100);
        CHECK(Sense_Faults() & SENSE_FAULT_UNDERVOLTAGE, "manual limit trips");
        Sense_ClearFaults();
        rail_mv(0);    ticks(100);
        rail_mv(8000); ticks(500);
        CHECK(limit_mv() == 4500, "manual limit kept, not replaced by auto");
        break;

    case 24: /* regression: overcurrent path untouched */
        power_up(6000);
        printf("overcurrent on a 6 V supply\n");
        rail_ma(6000); ticks(100);
        CHECK(Sense_Faults() == SENSE_FAULT_OVERCURRENT, "overcurrent latched, and only that (faults=%u)", Sense_Faults());
        CHECK(g_disable_all_calls >= 1, "Servo_DisableAll() called");
        break;

    default:
        return 2;
    }
    return failures ? 1 : 0;
}
