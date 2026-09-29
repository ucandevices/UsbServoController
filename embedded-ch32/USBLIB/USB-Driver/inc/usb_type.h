/********************************** (C) COPYRIGHT *******************************
 * File Name          : usb_type.h
 * Author             : WCH
 * Version            : V1.0.0
 * Date               : 2021/08/08
 * Description        : This file contains all the functions prototypes for the  
 *                      USB types firmware library.
*********************************************************************************
* Copyright (c) 2021 Nanjing Qinheng Microelectronics Co., Ltd.
* Attention: This software (modified or not) and binary are used for 
* microcontroller manufactured by Nanjing Qinheng Microelectronics.
*******************************************************************************/ 
#ifndef __USB_TYPE_H
#define __USB_TYPE_H
#include "debug.h"
#include "usb_conf.h"

#ifndef NULL
#define NULL ((void *)0)
#endif

/* PORT PATCH (servo controller): WCH defines its own `bool` enum here, which
   collides with <stdbool.h> (and is outright illegal from C23 on, where bool is
   a keyword). Use the standard type and keep FALSE/TRUE as macros so the rest
   of the USB library is untouched. Original kept as usb_type.h.orig. */
#include <stdbool.h>

#ifndef FALSE
#define FALSE false
#endif
#ifndef TRUE
#define TRUE  true
#endif

#endif /* __USB_TYPE_H */





