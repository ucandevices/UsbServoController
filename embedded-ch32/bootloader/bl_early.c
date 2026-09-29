/********************************************************************************
 * bl_early.c -- the bootloader's reset hook: stay, or start the application.
 *
 * Called from handle_reset (../Startup) with only sp and gp set up: no .data,
 * no .bss, no SystemInit, still in machine mode. Starting the application from
 * here hands it a chip in reset state, so its own startup code runs exactly as
 * if it had been reset into directly. Direct register access only.
 ********************************************************************************/
#include "ch32v20x.h"
#include "bootflag.h"
#include "flash_layout.h"

/* The application's first word is "j handle_reset", a JAL. Erased flash reads
   0xFFFFFFFF or 0xE339E339, neither of which has the JAL opcode, and the
   flasher writes this word last -- so an interrupted flash never looks valid. */
#define RV_OPCODE_MASK  0x7Fu
#define RV_OPCODE_JAL   0x6Fu

void BootFlag_CheckAndJump(void)
{
    uint8_t  requested;
    uint32_t first_word = *(volatile uint32_t *)APP_BASE;

    RCC->APB1PCENR |= RCC_PWREN | RCC_BKPEN;
    PWR->CTLR      |= PWR_CTLR_DBP;

    requested = (BKP->DATAR1 == BOOTFLAG_MAGIC1) &&
                (BKP->DATAR2 == BOOTFLAG_MAGIC2);
    if (requested)
    {
        /* Clear first: one attempt only, whatever happens next. */
        BKP->DATAR1 = 0u;
        BKP->DATAR2 = 0u;
    }

    PWR->CTLR      &= ~PWR_CTLR_DBP;
    RCC->APB1PCENR &= ~(RCC_PWREN | RCC_BKPEN);

    if (!requested && (first_word & RV_OPCODE_MASK) == RV_OPCODE_JAL)
    {
        ((void (*)(void))APP_BASE)();
    }
    /* Otherwise return, and the bootloader's own startup continues. */
}
