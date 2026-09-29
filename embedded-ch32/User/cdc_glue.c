/********************************************************************************
 * cdc_glue.c -- see cdc_glue.h. Replaces the SimulateCDC example's UART.c.
 ********************************************************************************/

#include "cdc_glue.h"

/* 115200 8N1, little-endian baud. Cosmetic: no UART is attached. */
volatile uint8_t CDC_LineCoding[8] = { 0x00, 0xC2, 0x01, 0x00, 0x00, 0x00, 0x08, 0x00 };

void CDC_LineCodingUpdated(void)
{
    /* Nothing to reconfigure -- the CDC port is the command channel, not a
       bridge to a physical UART. Retained as the hook usb_prop.c calls. */
}
