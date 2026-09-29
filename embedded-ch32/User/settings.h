/**
  ******************************************************************************
  * @file    settings.h
  * @brief   Per-channel settings kept in flash across power cycles.
  *
  * One 256-byte page at SETTINGS_BASE (flash_layout.h) holds, per channel, the
  * travel limits (servo.h) and the Maestro power-up speed and acceleration
  * (maestro.h). Changes take effect at once in RAM; they survive power-off only
  * after Settings_Save() -- the ASCII 'W' command -- so experimenting never
  * wears the flash. A firmware update keeps them: the bootloader erases only
  * the application region.
  ******************************************************************************
  */

#ifndef __SETTINGS_H
#define __SETTINGS_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdbool.h>

/** @brief Apply the stored settings, if a valid page is present. Call once,
  *        after Servo_Init() and before anything drives a servo. */
void Settings_Init(void);

/** @brief Write the settings in force to flash. */
bool Settings_Save(void);

/** @brief Back to factory defaults, in RAM and in flash. */
bool Settings_Reset(void);

#ifdef __cplusplus
}
#endif

#endif /* __SETTINGS_H */
