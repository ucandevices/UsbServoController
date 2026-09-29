/********************************************************************************
 * bootflag.c -- ask the board's USB bootloader to stay resident.
 *
 * See bootflag.h. The matching check lives in bootloader/bl_early.c.
 ********************************************************************************/
#include "ch32v20x.h"
#include "bootflag.h"
#include "flash_layout.h"

void BootFlag_RequestBootloader(void)
{
    __disable_irq();

    RCC->APB1PCENR |= RCC_PWREN | RCC_BKPEN;
    PWR->CTLR      |= PWR_CTLR_DBP;
    BKP->DATAR1 = BOOTFLAG_MAGIC1;
    BKP->DATAR2 = BOOTFLAG_MAGIC2;

    NVIC_SystemReset();
    while (1) { }
}
