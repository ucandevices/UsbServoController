/**
  ******************************************************************************
  * @file    protocol.h
  * @brief   ASCII command protocol over the USB CDC port.
  *
  * Line-based, '\n' terminated, case-insensitive. One reply line per command.
  * Chosen over the Maestro's binary protocol because it is debuggable from any
  * terminal; a binary compatibility layer can be added alongside it later.
  *
  *   S <ch> <us>   set channel pulse width in microseconds (500..2500)
  *   G <ch>        get channel pulse width; 0 means disabled
  *   E <ch> <0|1>  disable / enable a channel's output
  *   A <ch>        read channel as analog input (channels 0..7 only)
  *   X             disable all channels (panic stop)
  *   V             report firmware version and channel count
  *   BOOT          reset into the WCH factory USB bootloader
  *
  * Servo-rail monitoring (see sense.h; no Maestro has any of this):
  *
  *   I             rail current in mA
  *   U             rail voltage in mV
  *   N <0|1>       raw ADC counts, 0 = I_SENSE (PA5), 1 = V_SENSE (PA4)
  *   Z             zero the current sense; disables all channels, replies with
  *                 the stored offset in counts
  *   F             "OK <faults> <mA> <mV> <rail_present>" -- faults is a
  *                 bitmask, bit0 overcurrent, bit1 undervoltage
  *   C             clear latched faults
  *   P <mA> <mV>   set overcurrent / undervoltage limits, 0 disables either;
  *                 leaves undervoltage AUTO mode until the next power-up
  *   L             limits in force: OK <mA> <mV> <auto>, auto=1 while the
  *                 undervoltage limit is chosen from the detected supply
  *
  * While a fault is latched, both "E <ch> 1" and "S <ch> <us>" are refused with
 * ERR fault -- "S" enables the channel too, so it is a second way in.
 * Clear the latch with "C" once the cause is understood.
  *
  * Replies: "OK", "OK <value>", or "ERR <reason>".
  ******************************************************************************
  */

#ifndef __PROTOCOL_H
#define __PROTOCOL_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>

#define PROTO_VERSION_STRING "USBServoController 0.3 12ch CH32V203 isense"

/**
  * @brief Feed bytes received on the CDC OUT endpoint into the parser.
  * @note  Called from CDC_Receive_FS, i.e. in USB interrupt context. It only
  *        buffers; complete lines are executed later from Protocol_Task() in
  *        the main loop, so command handling never runs at interrupt priority.
  */
void Protocol_RxData(const uint8_t *data, uint32_t len);

/** @brief Execute any complete lines that have arrived. Call from main loop. */
void Protocol_Task(void);

/** @brief True once a BOOT command has been accepted and acknowledged. */
uint8_t Protocol_BootRequested(void);

/**
  * @brief Detach USB, set the boot flag and reset into the ROM bootloader.
  * @note  Never returns. See bootflag.h for the mechanism.
  */
void Protocol_JumpToBootloader(void);

#ifdef __cplusplus
}
#endif

#endif /* __PROTOCOL_H */
