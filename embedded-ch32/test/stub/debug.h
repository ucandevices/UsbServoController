/* Host-test stand-in for WCH's debug.h: just enough of the HAL for sense.c. */
#ifndef HOSTTEST_DEBUG_H
#define HOSTTEST_DEBUG_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum { DISABLE = 0, ENABLE = 1 } FunctionalState;
typedef enum { RESET = 0, SET = 1 } FlagStatus;

typedef struct { int dummy; } ADC_TypeDef;
typedef struct { int dummy; } GPIO_TypeDef;
extern ADC_TypeDef  hosttest_adc1;
extern GPIO_TypeDef hosttest_gpioa;
#define ADC1  (&hosttest_adc1)
#define GPIOA (&hosttest_gpioa)

typedef struct {
    uint32_t ADC_Mode;
    FunctionalState ADC_ScanConvMode;
    FunctionalState ADC_ContinuousConvMode;
    uint32_t ADC_ExternalTrigConv;
    uint32_t ADC_DataAlign;
    uint8_t  ADC_NbrOfChannel;
} ADC_InitTypeDef;

typedef struct {
    uint16_t GPIO_Pin;
    uint32_t GPIO_Speed;
    uint32_t GPIO_Mode;
} GPIO_InitTypeDef;

#define RCC_APB2Periph_ADC1        0x200u
#define RCC_PCLK2_Div8             3u
#define GPIO_Pin_4                 0x10u
#define GPIO_Pin_5                 0x20u
#define GPIO_Mode_AIN              0u
#define ADC_Mode_Independent       0u
#define ADC_ExternalTrigConv_None  0u
#define ADC_DataAlign_Right        0u
#define ADC_Channel_4              4u
#define ADC_Channel_5              5u
#define ADC_SampleTime_239Cycles5  7u
#define ADC_FLAG_EOC               2u

void       RCC_APB2PeriphClockCmd(uint32_t p, FunctionalState s);
void       RCC_ADCCLKConfig(uint32_t d);
void       GPIO_Init(GPIO_TypeDef *g, GPIO_InitTypeDef *i);
void       ADC_Init(ADC_TypeDef *a, ADC_InitTypeDef *i);
void       ADC_Cmd(ADC_TypeDef *a, FunctionalState s);
void       ADC_ResetCalibration(ADC_TypeDef *a);
FlagStatus ADC_GetResetCalibrationStatus(ADC_TypeDef *a);
void       ADC_StartCalibration(ADC_TypeDef *a);
FlagStatus ADC_GetCalibrationStatus(ADC_TypeDef *a);
void       ADC_RegularChannelConfig(ADC_TypeDef *a, uint8_t ch, uint8_t rank, uint8_t st);
void       ADC_SoftwareStartConvCmd(ADC_TypeDef *a, FunctionalState s);
FlagStatus ADC_GetFlagStatus(ADC_TypeDef *a, uint8_t f);
uint16_t   ADC_GetConversionValue(ADC_TypeDef *a);

#endif
