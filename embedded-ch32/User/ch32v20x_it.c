/********************************************************************************
 * ch32v20x_it.c -- interrupt handlers.
 *
 * The SimulateCDC example put a TIM2 handler here to time the UART bridge.
 * TIM2 drives servo channels 0-3 on this board, so that handler is gone.
 * USB interrupts are handled inside USBLIB/CONFIG/hw_config.c.
 ********************************************************************************/

#include "ch32v20x_it.h"

void NMI_Handler(void)       __attribute__((interrupt("machine")));
void HardFault_Handler(void) __attribute__((interrupt("machine")));

void NMI_Handler(void)
{
    while (1) { }
}

void HardFault_Handler(void)
{
    while (1) { }
}
