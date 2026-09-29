/********************************************************************************
 * cdc_glue.h
 *
 * WCH's USBD SimulateCDC example is a USB<->UART bridge, so its USB library
 * reaches into a UART control struct for the CDC line-coding bytes and routes
 * OUT-endpoint data straight into a serial TX ring.
 *
 * This board has no UART -- the CDC port is the command channel itself. This
 * header replaces the example's UART.h with the minimum the USB library still
 * needs, so usb_prop.c and usb_endp.c compile unmodified in spirit: a
 * line-coding buffer the host can read/write, and nothing else.
 ********************************************************************************/

#ifndef __CDC_GLUE_H
#define __CDC_GLUE_H

#ifdef __cplusplus
extern "C" {
#endif

#include "debug.h"

#define DEF_USB_FS_PACK_LEN   64u    /* USB FS bulk packet size */

/* CDC SET_LINE_CODING / GET_LINE_CODING payload:
   4 bytes baud (LE), 1 stop bits, 1 parity, 1 data bits. The host insists on
   these existing; nothing here acts on them, because there is no UART behind
   the port. Kept so terminal programs open the port without complaining. */
extern volatile uint8_t CDC_LineCoding[8];

/* Called by usb_prop.c when the host issues SET_LINE_CODING. */
void CDC_LineCodingUpdated(void);

/* Endpoint 3 IN busy flag, owned by usb_endp.c. */
extern volatile uint8_t USBD_Endp3_Busy;

/* Queue len bytes to the host on the CDC IN endpoint.
   Returns USB_SUCCESS, or USB_ERROR if the previous packet is still in flight. */
uint8_t USBD_ENDPx_DataUp(uint8_t endp, uint8_t *pbuf, uint16_t len);

#ifdef __cplusplus
}
#endif

#endif /* __CDC_GLUE_H */
