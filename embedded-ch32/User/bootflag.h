/********************************************************************************
 * bootflag.h -- software entry into the board's own USB bootloader.
 *
 * The CH32V203 latches BOOT0/BOOT1 at reset and has no software boot-mode bit,
 * and its ROM bootloader refuses to run when entered by a jump with BOOT0 low
 * (tried on hardware 2026-09-28: it drops straight back into user flash). So
 * the board carries its own bootloader in the first 12 KB of flash
 * (bootloader/, see flash_layout.h), which runs on every reset.
 *
 * To reach it, the application leaves a magic value in BKP_DATAR1/2 (backup
 * registers survive a system reset) and resets. The bootloader's reset hook
 * sees the flag, clears it, and stays resident instead of starting the
 * application. The flag is one-shot, so the board cannot get stuck in the
 * bootloader: any later reset starts the application again.
 ********************************************************************************/
#ifndef BOOTFLAG_H
#define BOOTFLAG_H

/**
  * @brief Set the flag and reset into the bootloader. Never returns.
  * @note  The caller should quiesce outputs and detach USB first.
  */
void BootFlag_RequestBootloader(void) __attribute__((noreturn));

/**
  * @brief Reset hook, called from the reset handler with only sp/gp set up.
  *        The application gets a weak no-op from the startup file; the
  *        bootloader's version decides between staying and starting the app.
  * @note  Must not touch globals: .data and .bss are not initialised yet.
  */
void BootFlag_CheckAndJump(void);

#endif /* BOOTFLAG_H */
