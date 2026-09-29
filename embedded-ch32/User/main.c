/********************************************************************************
 * main.c -- USB-C 12-Channel Servo Controller, CH32V203C8T6 port.
 *
 * Feasibility port of the STM32F072 firmware in ../embedded. Same board, same
 * schematic, same command protocol; different silicon vendor.
 *
 * Clock: HSE 8 MHz (crystal Y1) x12 = 96 MHz SYSCLK, USB = PLL/2 = 48 MHz.
 ********************************************************************************/

#include "debug.h"
#include "usb_lib.h"
#include "hw_config.h"

#include "servo.h"
#include "sense.h"
#include "protocol.h"
#include "script.h"

/* Status LED D3: PC13 -> R13 -> anode, cathode to GND, so on = pin high.
   One pattern at a time, highest priority first; t is main-loop ticks (~1 ms).
     fault latched         10 Hz even flashing -- needs a clear (C command)
     script running        steady on
     idle, supply present  one short blip per second
     idle, no servo supply two short blips per second
   The bootloader (bootloader/) blinks an even 5 Hz, distinct from all four. */
static bool led_pattern(uint32_t t)
{
    uint32_t p = t % 1000u;

    if (Sense_Faults() != 0u)
    {
        return ((t / 50u) & 1u) == 0u;
    }
    if (Script_Running())
    {
        return true;
    }
    if (Sense_RailPresent())
    {
        return p < 60u;
    }
    return p < 60u || (p >= 200u && p < 260u);
}

static void led_init(void)
{
    GPIO_InitTypeDef gpio = {0};

    RCC_APB2PeriphClockCmd(RCC_APB2Periph_GPIOC, ENABLE);
    gpio.GPIO_Pin   = GPIO_Pin_13;          /* PC13 -> R13 -> D3 */
    gpio.GPIO_Mode  = GPIO_Mode_Out_PP;
    gpio.GPIO_Speed = GPIO_Speed_2MHz;
    GPIO_Init(GPIOC, &gpio);
}

int main(void)
{
    uint32_t tick = 0;

    NVIC_PriorityGroupConfig(NVIC_PriorityGroup_1);
    SystemCoreClockUpdate();
    Delay_Init();

    led_init();

    /* Bind the servo outputs and start all 12 timers. Every channel comes up
       disabled (idle low) until the host commands a position. */
    Servo_Init();

    /* ADC1 plus the two rail-sense pins (PA4 V_SENSE, PA5 I_SENSE). Must come
       after Servo_Init() so nothing is driving a servo pin while the ADC
       calibrates, and before the main loop starts monitoring. */
    Sense_Init();

    /* A stored script marked autorun starts here, with or without a PC. */
    Script_Init();

    /* USB CDC: the command channel. */
    Set_USBConfig();
    USB_Init();
    USB_Interrupts_Config();

    while (1)
    {
        /* Execute any complete command lines buffered by the USB ISR. */
        Protocol_Task();

        /* Watch the servo rail. Trips drop every channel, which de-energises
           the servos and so removes the current -- the board has no series
           protection element, this is what stands in for one. */
        Sense_Task();

        /* Stored script: ramps and instructions, timed off TIM2. */
        Script_Task();

        /* A BOOT command hands the chip to the factory USB bootloader. The
           jump happens here, out of interrupt context, after the reply is sent. */
        if (Protocol_BootRequested())
        {
            Protocol_JumpToBootloader();    /* does not return */
        }

        /* Status LED: alive, and what state the board is in -- see led_pattern(). */
        GPIO_WriteBit(GPIOC, GPIO_Pin_13, led_pattern(tick++) ? Bit_SET : Bit_RESET);
        Delay_Ms(1);
    }
}
