/********************************************************************************
 * servo.h -- 12-channel servo output driver, CH32V203C8T6 port.
 *
 * Channel -> timer mapping follows the same schematic as the STM32 build
 * (PCB/usbservocontroller). The CH32V203 is STM32F103-pin-compatible, so every
 * servo pin keeps its board position; only the timer behind channel 11 differs,
 * because the CH32V203 has TIM4 where the STM32F072 had TIM16:
 *
 *   ch0..ch3   PA0,PA1,PA2,PA3   TIM2_CH1..CH4
 *   ch4..ch7   PA6,PA7,PB0,PB1   TIM3_CH1..CH4
 *   ch8..ch10  PA8,PA9,PA10      TIM1_CH1..CH3
 *   ch11       PB8               TIM4_CH3      (was TIM16_CH1 on STM32F072)
 *
 * Clock tree (see main.c): HSE 8 MHz x12 = 96 MHz SYSCLK.
 *   APB2 /1 -> 96 MHz -> TIM1
 *   APB1 /2 -> 48 MHz, x2 timer rule -> 96 MHz -> TIM2/3/4
 * So all four timers see 96 MHz; PSC=47 gives a 2 MHz tick (0.5 us) and
 * ARR=39999 gives a 20.000 ms frame (50 Hz), identical to the STM32 build.
 ********************************************************************************/

#ifndef __SERVO_H
#define __SERVO_H

#ifdef __cplusplus
extern "C" {
#endif

#include "debug.h"
#include <stdbool.h>
#include <stdint.h>

#define SERVO_CHANNELS      12u

#define SERVO_TIMER_HZ      2000000u
#define SERVO_TICKS_PER_US  (SERVO_TIMER_HZ / 1000000u)   /* = 2 */
#define SERVO_FRAME_TICKS   40000u

#define SERVO_US_MIN        500u
#define SERVO_US_MAX        2500u
#define SERVO_US_NEUTRAL    1500u

void     Servo_Init(void);
bool     Servo_SetPulseUs(uint8_t ch, uint16_t us);
uint16_t Servo_GetPulseUs(uint8_t ch);
/* Last width commanded, even while the channel is off (1500 if never set):
   where an unpowered servo was most likely left. */
uint16_t Servo_LastPulseUs(uint8_t ch);
bool     Servo_SetEnabled(uint8_t ch, bool enabled);
bool     Servo_IsEnabled(uint8_t ch);
void     Servo_DisableAll(void);

bool     Servo_HasAnalog(uint8_t ch);
bool     Servo_ReadAnalog(uint8_t ch, uint16_t *value);
/* PA4/PA5 are no longer spare analog inputs -- they are the rail voltage and
   current sense pins. See sense.h. */

#ifdef __cplusplus
}
#endif

#endif /* __SERVO_H */
