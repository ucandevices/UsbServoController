/**
  ******************************************************************************
  * @file    maestro.h
  * @brief   Pololu Maestro serial protocols over the same USB CDC port.
  *
  * Runs alongside the ASCII protocol (protocol.h). A byte of 0x80 or above
  * starts a Maestro command; printable ASCII never does, so the two never
  * collide and a Maestro client library can open the port and just work.
  *
  *   Compact   0x84 ch lo hi                 command byte first
  *   Pololu    0xAA 12 0x04 ch lo hi         device number 12, command & 0x7F
  *   Mini SSC  0xFF servo target             target 0..254, 127 = 1500 us
  *
  * Commands (compact byte):
  *   0x84 Set Target          ch, target in quarter-us (0 = channel off)
  *   0x9F Set Multiple Targets count, first ch, count x target
  *   0x87 Set Speed           ch, (0.25 us)/(10 ms), 0 = unlimited
  *   0x89 Set Acceleration    ch, (0.25 us)/(10 ms)/(80 ms), 0 = unlimited
  *   0x90 Get Position        ch -> 2 bytes, quarter-us, little-endian
  *   0x93 Get Moving State    -> 1 byte, 1 while any channel is still ramping
  *   0xA1 Get Errors          -> 2 bytes, then cleared
  *   0xA2 Go Home             all channels off (the Maestro's default home)
  *   0xA4 Stop Script         stops the board's stored script (script.h)
  *   0xAE Get Script Status   -> 1 byte, 0 = running, 1 = stopped
  *   0x8A Set PWM, 0xA7/0xA8 Restart Script: accepted and ignored
  *
  * Targets clamp to each channel's travel limits (servo.h, set with the ASCII
  * 'R' command, 500..2500 us by default, up to 64..4080 us) and are output at
  * 0.5 us resolution. Limits and power-up speed/acceleration persist with 'W'
  * (settings.h). No CRC or serial timeout; Maestro Control Center cannot
  * connect (it needs Pololu's USB IDs).
  * A move never re-energises a channel while a rail fault is latched.
  ******************************************************************************
  */

#ifndef __MAESTRO_H
#define __MAESTRO_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdbool.h>
#include <stdint.h>

#define MAESTRO_DEVICE_NUMBER   12u

/* Maestro error register bits reported by Get Errors. */
#define MAESTRO_ERR_PROTOCOL    0x0010u

typedef void (*MaestroSend_t)(const uint8_t *data, uint16_t len);

/**
  * @brief  Offer one received byte to the Maestro parser.
  * @retval true if the byte belongs to a Maestro command (consumed); false if
  *         it is ASCII for the line protocol.
  */
bool Maestro_Feed(uint8_t c, MaestroSend_t send);

/** @brief Advance speed/acceleration ramps. Call from the main loop. */
void Maestro_Task(void);

/** @brief Stop any Maestro ramp on one channel (another command took it over). */
void Maestro_Cancel(uint8_t ch);

/** @brief Stop every Maestro ramp. */
void Maestro_CancelAll(void);

/* Speed and acceleration per channel, in the Maestro's units; settings.c
   stores them as the power-up values. */
uint16_t Maestro_GetSpeed(uint8_t ch);
uint16_t Maestro_GetAccel(uint8_t ch);
void     Maestro_SetSpeed(uint8_t ch, uint16_t speed);
void     Maestro_SetAccel(uint8_t ch, uint16_t accel);

#ifdef __cplusplus
}
#endif

#endif /* __MAESTRO_H */
