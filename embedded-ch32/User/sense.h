/********************************************************************************
 * sense.h -- servo-rail current and voltage sensing, CH32V203C8T6.
 *
 * Hardware (see PCB/usbservocontroller, README section 2.2):
 *
 *   current   J2 -- R18 5 mOhm shunt -- SERVO_V+
 *             U4 INA180A2 measures across R18, high side, gain 50 V/V
 *             U4 OUT -> PA5 = ADC_Channel_5   (net I_SENSE)
 *
 *   voltage   SERVO_V+ -- R19..R22, four 5k1 in series, tap between R21/R22
 *             tap -> PA4 = ADC_Channel_4      (net V_SENSE), C18 100nF filter
 *
 * These are the two pins that used to be the J11 spare analog header. J11 is
 * gone; PA4/PA5 now monitor the board instead. Neither exists on any Maestro.
 *
 * Scaling -- the two chains happen to share a full-scale constant:
 *
 *   current   1 LSB = 3.3 V / 4095 across a 0.005 * 50 = 0.25 V/A chain
 *             => full scale 3.3 / 0.25 = 13.2 A
 *   voltage   divider is /4, so full scale 3.3 * 4 = 13.2 V
 *
 * so both are simply  units = raw * 13200 / 4095  (mA and mV respectively).
 * Keep SENSE_FULL_SCALE in step with R18, the INA gain and the divider ratio.
 *
 * Accuracy is bounded by VDDA, because LQFP-48 has no VREF+ pin and VDDA is
 * therefore the ADC reference. That is the HT7533-1's output tolerance. Good
 * enough for threshold decisions, not for a calibrated meter.
 ********************************************************************************/

#ifndef __SENSE_H
#define __SENSE_H

#ifdef __cplusplus
extern "C" {
#endif

#include "debug.h"
#include <stdbool.h>
#include <stdint.h>

/* Latched fault bits, as reported by the 'F' command. */
#define SENSE_FAULT_OVERCURRENT   0x01u
#define SENSE_FAULT_UNDERVOLTAGE  0x02u

/* Raw-channel selectors for Sense_ReadRaw() and the 'N' command. */
#define SENSE_RAW_CURRENT         0u
#define SENSE_RAW_VOLTAGE         1u

/* Overcurrent default matches the ~5 A the 2.4 mm rail can actually carry.
   Setting it to 0 with the 'P' command disables it. */
#define SENSE_DEFAULT_LIMIT_MA    5000u

/* Undervoltage starts in AUTO mode. A fixed default cannot suit every supply:
   the old 6.0 V limit latched a fault within a fraction of a second on any
   5 V or 6 V supply -- the most common servo setup -- and blocked every
   channel until the host intervened. Instead, once a supply has read steady
   for SENSE_UV_SETTLE_READS task periods, it is classified by voltage:

     SENSE_UV_2S_MIN_MV .. SENSE_UV_2S_MAX_MV   2S LiPo -> cut off at 3.2 V/cell
     SENSE_UV_3S_MIN_MV .. (sense full scale)   3S LiPo -> cut off at 3.2 V/cell
     anything else                              BEC, NiMH, bench -> no cutoff

   The 2S band starts at 6.6 V so a 6 V BEC (6.0-6.3 V with tolerance) is never
   mistaken for a nearly flat pack. Removing the supply resets the
   classification, so a pack swap is re-detected. Any 'P' command switches to
   manual limits until the next power-up. */
#define SENSE_UV_SETTLE_READS     10u        /* ~100 ms at SENSE_TASK_PERIOD_MS */
#define SENSE_UV_2S_MIN_MV        6600u
#define SENSE_UV_2S_MAX_MV        8800u      /* 2S HV-LiPo tops out at 8.7 V   */
#define SENSE_UV_2S_LIMIT_MV      6400u
#define SENSE_UV_3S_MIN_MV        9900u
#define SENSE_UV_3S_LIMIT_MV      9600u

/* Below this the servo supply is treated as absent rather than sagging, so a
   USB-only board does not undervoltage-fault the moment it powers up. */
#define SENSE_RAIL_PRESENT_MV     2000u

/** @brief Initialise ADC1 and both sense channels. Call once, before Servo
  *        analog reads -- servo.c shares this ADC through Sense_SampleAdc(). */
void Sense_Init(void);

/** @brief Single blocking conversion on any ADC1 regular channel.
  * @note  Shared primitive: servo.c uses it for the per-channel 'A' command.
  *        Everything that calls it runs from the main loop, so there is no
  *        preemption and no need to lock the ADC. */
bool Sense_SampleAdc(uint8_t adc_channel, uint16_t *raw);

/** @brief Averaged raw counts. idx is SENSE_RAW_CURRENT or SENSE_RAW_VOLTAGE. */
bool Sense_ReadRaw(uint8_t idx, uint16_t *raw);

/** @brief Rail current in mA, zero-offset corrected. Never returns negative:
  *        the INA180A2 is unidirectional and current only flows J2 -> servos. */
bool Sense_ReadCurrent_mA(uint32_t *ma);

/** @brief Servo rail voltage in mV, referred to the far side of the shunt. */
bool Sense_ReadVoltage_mV(uint32_t *mv);

/** @brief Measure and store the INA180A2's zero-current output offset.
  * @note  Disables every servo channel first, because the reading is only
  *        meaningful with no load. A few LSB is normal. */
bool Sense_TareCurrent(uint16_t *offset_out);

uint16_t Sense_CurrentOffset(void);

/** @brief Set both limits explicitly; 0 disables either. Leaves undervoltage
  *        AUTO mode for the rest of this power cycle. */
void Sense_SetLimits(uint32_t ma, uint32_t mv);

/** @brief Limits currently in force. In AUTO mode mv is the limit chosen for
  *        the detected supply, or 0 if none applies (or none detected yet). */
void Sense_GetLimits(uint32_t *ma, uint32_t *mv);

/** @brief True while the undervoltage limit is chosen automatically. */
bool Sense_UndervoltageAuto(void);

/** @brief Latched fault bits; 0 when healthy. */
uint8_t Sense_Faults(void);

/** @brief Clear latched faults. Does not re-enable any channel. */
void Sense_ClearFaults(void);

/** @brief True when the servo supply reads above SENSE_RAIL_PRESENT_MV. */
bool Sense_RailPresent(void);

uint32_t Sense_LastCurrent_mA(void);
uint32_t Sense_LastVoltage_mV(void);

/** @brief Periodic monitor. Call from the main loop at ~1 kHz; it rate-limits
  *        itself internally and ignores the first SENSE_STARTUP_GRACE_MS so the
  *        inrush into C15/C16 (940 uF downstream of the shunt) cannot trip it. */
void Sense_Task(void);

#ifdef __cplusplus
}
#endif

#endif /* __SENSE_H */
