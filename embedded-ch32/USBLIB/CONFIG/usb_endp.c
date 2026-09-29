/********************************************************************************
 * usb_endp.c -- CDC endpoint callbacks for the USB servo controller.
 *
 * Replaces WCH's SimulateCDC version, which piped the OUT endpoint into a UART
 * transmit ring. Here the OUT endpoint is the command channel: bytes go
 * straight into the protocol parser's ring buffer and are executed later from
 * the main loop, so nothing runs command logic at interrupt priority.
 ********************************************************************************/

#include "usb_lib.h"
#include "usb_desc.h"
#include "usb_mem.h"
#include "hw_config.h"
#include "usb_istr.h"
#include "usb_pwr.h"
#include "usb_prop.h"

#include "cdc_glue.h"
#include "protocol.h"

volatile uint8_t USBD_Endp3_Busy = 0;

/* Staging buffer for one OUT packet. */
static uint8_t s_rx_pkt[DEF_USB_FS_PACK_LEN];

void EP1_IN_Callback(void)
{
    /* Interrupt (notification) endpoint -- unused by this protocol. */
}

void EP2_OUT_Callback(void)
{
    uint32_t len = GetEPRxCount(EP2_OUT & 0x7F);

    if (len > DEF_USB_FS_PACK_LEN)
    {
        len = DEF_USB_FS_PACK_LEN;
    }

    PMAToUserBufferCopy(s_rx_pkt, GetEPRxAddr(EP2_OUT & 0x7F), len);
    Protocol_RxData(s_rx_pkt, len);

    /* Re-arm immediately: the parser only buffers, so we never need to hold
       off the host the way the UART bridge did. */
    SetEPRxValid(ENDP2);
}

void EP3_IN_Callback(void)
{
    USBD_Endp3_Busy = 0;
}

uint8_t USBD_ENDPx_DataUp(uint8_t endp, uint8_t *pbuf, uint16_t len)
{
    if (endp != ENDP3)
    {
        return USB_ERROR;
    }
    if (USBD_Endp3_Busy)
    {
        return USB_ERROR;
    }

    USB_SIL_Write(EP3_IN, pbuf, len);
    USBD_Endp3_Busy = 1;
    SetEPTxStatus(ENDP3, EP_TX_VALID);
    return USB_SUCCESS;
}
