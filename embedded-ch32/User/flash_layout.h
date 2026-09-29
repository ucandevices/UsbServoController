/********************************************************************************
 * flash_layout.h -- how the 64 KB of code flash is split.
 *
 *   0x0000 .. 0x2FFF   USB bootloader (bootloader/), 12 KB
 *   0x3000 .. 0xEFFF   application, 48 KB
 *   0xF000 .. 0xFFFF   stored script (script.c), 4 KB
 *
 * The linker scripts cannot include this header, so the same numbers are
 * repeated in Ld/Link.ld (application) and bootloader/Link.ld. Change all three
 * together. APP_BASE and SCRIPT_BASE must stay 4 KB aligned: they are also
 * erase boundaries. The bootloader erases only APP_SIZE, so a firmware update
 * keeps the stored script -- once the bootloader itself has been rebuilt with
 * this layout (an older one erases up to 0xFFFF).
 *
 * Addresses below are the 0x00000000 alias the CPU executes from. The flash
 * controller wants the real 0x08000000-based address -- use *_FLASH_ADDR.
 ********************************************************************************/

#ifndef __FLASH_LAYOUT_H
#define __FLASH_LAYOUT_H

#define CODE_FLASH_SIZE     0x10000u            /* CH32V203C8: 64 KB */
#define APP_BASE            0x00003000u
#define SCRIPT_BASE         0x0000F000u
#define SCRIPT_SIZE         (CODE_FLASH_SIZE - SCRIPT_BASE)
#define APP_SIZE            (SCRIPT_BASE - APP_BASE)
#define APP_FLASH_ADDR      (0x08000000u + APP_BASE)
#define SCRIPT_FLASH_ADDR   (0x08000000u + SCRIPT_BASE)

/* BKP_DATAR1/2 survive a system reset. The application writes these and resets
   to ask the bootloader to stay resident instead of starting the application. */
#define BOOTFLAG_MAGIC1     0xB007u
#define BOOTFLAG_MAGIC2     0x10ADu

#endif /* __FLASH_LAYOUT_H */
