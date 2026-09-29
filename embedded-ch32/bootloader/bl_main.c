/********************************************************************************
 * bl_main.c -- USB CDC bootloader for the servo controller.
 *
 * Runs when the application asks for it (BOOT command -> BKP flag -> reset) or
 * when no valid application is present; see bl_early.c. Enumerates as the same
 * CDC port as the application (same VID/PID, no driver needed) and speaks a
 * line protocol, one reply per command, driven by tools/flash.py:
 *
 *   V                  OK BOOTLOADER 1 <app base hex> <app size hex>
 *   BOOT               OK BOOT             (already here; lets the host always
 *                                           send BOOT without checking first)
 *   E                  OK                  erase the whole application region
 *   W <off> <hex*512>  OK                  program 256 bytes at app base + off
 *                                           (off hex, 256-aligned), read back
 *   C <len>            OK <crc32 hex>      CRC-32 (zlib) of the first len bytes
 *   G                  OK                  detach, reset, start the application
 *
 * Errors reply "ERR <reason>". The LED blinks at 5 Hz while resident, twice
 * the application's rate, so the mode is visible without a terminal.
 *
 * Offsets are relative to APP_BASE, so the host never needs the memory map.
 * The host writes page 0 last: until then the reset vector reads as erased
 * and bl_early.c keeps the board here, so an interrupted flash is harmless.
 ********************************************************************************/

#include <string.h>

#include "debug.h"
#include "usb_lib.h"
#include "usb_desc.h"
#include "usb_pwr.h"
#include "hw_config.h"
#include "cdc_glue.h"
#include "protocol.h"
#include "flash_layout.h"

#define BL_PROTOCOL_VERSION  "1"
#define PAGE_SIZE            256u
#define LINE_MAX             (16u + 2u * PAGE_SIZE)
#define RX_RING_SIZE         1024u      /* > one full W line; host waits per reply */
#define LED_TOGGLE_LOOPS     1000u      /* x 100 us = 100 ms */

static volatile uint8_t  s_ring[RX_RING_SIZE];
static volatile uint16_t s_head;        /* written by the USB ISR */
static volatile uint16_t s_tail;        /* written by the main loop */

static char     s_line[LINE_MAX + 1u];
static uint16_t s_line_len;
static uint8_t  s_line_overflow;

static uint32_t s_page[PAGE_SIZE / 4u];

/* --- USB side ----------------------------------------------------------- */

/* Called by usb_endp.c from the USB ISR with each OUT packet. */
void Protocol_RxData(const uint8_t *data, uint32_t len)
{
    while (len--)
    {
        uint16_t next = (uint16_t)((s_head + 1u) % RX_RING_SIZE);
        if (next == s_tail)
        {
            return;     /* full: drop. Cannot happen with one line in flight. */
        }
        s_ring[s_head] = *data++;
        s_head = next;
    }
}

static void reply(const char *s)
{
    uint16_t len = (uint16_t)strlen(s);
    uint32_t guard;

    for (guard = 0; guard < 100000u; guard++)
    {
        if (USBD_ENDPx_DataUp(ENDP3, (uint8_t *)s, len) == USB_SUCCESS)
        {
            return;
        }
    }
}

static void reply_err(const char *reason)
{
    char b[48] = "ERR ";
    strncat(b, reason, sizeof b - 7u);
    strcat(b, "\r\n");
    reply(b);
}

/* --- helpers ------------------------------------------------------------ */

static int hex_digit(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* Parse one hex number, advancing *p past it and any trailing spaces. */
static int parse_hex(const char **p, uint32_t *out)
{
    const char *s = *p;
    uint32_t v = 0;
    int n = 0, d;

    while (*s == ' ') s++;
    while ((d = hex_digit(*s)) >= 0 && n < 8)
    {
        v = (v << 4) | (uint32_t)d;
        s++;
        n++;
    }
    if (n == 0 || hex_digit(*s) >= 0)
    {
        return 0;
    }
    while (*s == ' ') s++;
    *p = s;
    *out = v;
    return 1;
}

static void format_hex(char *out, uint32_t v)
{
    static const char digits[] = "0123456789ABCDEF";
    int i;
    for (i = 7; i >= 0; i--)
    {
        out[i] = digits[v & 0xFu];
        v >>= 4;
    }
    out[8] = '\0';
}

static uint32_t crc32(const uint8_t *p, uint32_t len)
{
    uint32_t crc = 0xFFFFFFFFu;
    int k;

    while (len--)
    {
        crc ^= *p++;
        for (k = 0; k < 8; k++)
        {
            crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
        }
    }
    return ~crc;
}

/* --- commands ----------------------------------------------------------- */

static void cmd_version(void)
{
    char b[48] = "OK BOOTLOADER " BL_PROTOCOL_VERSION " ";
    char h[9];

    format_hex(h, APP_BASE);
    strcat(b, h);
    strcat(b, " ");
    format_hex(h, APP_SIZE);
    strcat(b, h);
    strcat(b, "\r\n");
    reply(b);
}

static void cmd_erase(void)
{
    if (FLASH_ROM_ERASE(APP_FLASH_ADDR, APP_SIZE) != FLASH_COMPLETE)
    {
        reply_err("erase");
        return;
    }
    reply("OK\r\n");
}

static void cmd_write(const char *args)
{
    uint32_t off, i;
    uint8_t *bytes = (uint8_t *)s_page;

    if (!parse_hex(&args, &off) || (off % PAGE_SIZE) != 0u || off >= APP_SIZE)
    {
        reply_err("offset");
        return;
    }
    if (strlen(args) != 2u * PAGE_SIZE)
    {
        reply_err("length");
        return;
    }
    for (i = 0; i < PAGE_SIZE; i++)
    {
        int hi = hex_digit(args[2u * i]);
        int lo = hex_digit(args[2u * i + 1u]);
        if (hi < 0 || lo < 0)
        {
            reply_err("hex");
            return;
        }
        bytes[i] = (uint8_t)((hi << 4) | lo);
    }

    if (FLASH_ROM_WRITE(APP_FLASH_ADDR + off, s_page, PAGE_SIZE) != FLASH_COMPLETE)
    {
        reply_err("write");
        return;
    }
    if (memcmp((const void *)(APP_FLASH_ADDR + off), s_page, PAGE_SIZE) != 0)
    {
        reply_err("verify");
        return;
    }
    reply("OK\r\n");
}

static void cmd_crc(const char *args)
{
    uint32_t len;
    char b[16] = "OK ";

    if (!parse_hex(&args, &len) || len > APP_SIZE)
    {
        reply_err("length");
        return;
    }
    format_hex(b + 3, crc32((const uint8_t *)APP_FLASH_ADDR, len));
    strcat(b, "\r\n");
    reply(b);
}

static void cmd_go(void)
{
    uint32_t guard;

    reply("OK\r\n");
    for (guard = 0; USBD_Endp3_Busy && guard < 50u; guard++)
    {
        Delay_Ms(1);
    }

    /* Clean detach so the host sees the port go before the application's
       enumerates. No flag is set, so bl_early.c starts the application. */
    USB_Port_Set(DISABLE, DISABLE);
    Delay_Ms(20);
    NVIC_SystemReset();
    while (1) { }
}

static void execute(char *line)
{
    char verb = line[0];

    if (verb >= 'a' && verb <= 'z')
    {
        verb = (char)(verb - 'a' + 'A');
    }

    if (strncmp(line, "BOOT", 4) == 0 || strncmp(line, "boot", 4) == 0)
    {
        reply("OK BOOT\r\n");
        return;
    }

    switch (verb)
    {
        case 'V': cmd_version();        break;
        case 'E': cmd_erase();          break;
        case 'W': cmd_write(line + 1);  break;
        case 'C': cmd_crc(line + 1);    break;
        case 'G': cmd_go();             break;
        default:  reply_err("unknown"); break;
    }
}

static void poll_input(void)
{
    while (s_tail != s_head)
    {
        char c = (char)s_ring[s_tail];
        s_tail = (uint16_t)((s_tail + 1u) % RX_RING_SIZE);

        if (c == '\r' || c == '\n')
        {
            if (s_line_overflow)
            {
                reply_err("line too long");
            }
            else if (s_line_len > 0u)
            {
                s_line[s_line_len] = '\0';
                execute(s_line);
            }
            s_line_len = 0;
            s_line_overflow = 0;
        }
        else if (s_line_len < LINE_MAX)
        {
            s_line[s_line_len++] = c;
        }
        else
        {
            s_line_overflow = 1;
        }
    }
}

/* --- main --------------------------------------------------------------- */

int main(void)
{
    GPIO_InitTypeDef gpio = {0};
    uint32_t led_loops = LED_TOGGLE_LOOPS;

    NVIC_PriorityGroupConfig(NVIC_PriorityGroup_1);
    SystemCoreClockUpdate();
    Delay_Init();

    RCC_APB2PeriphClockCmd(RCC_APB2Periph_GPIOC, ENABLE);
    gpio.GPIO_Pin   = GPIO_Pin_13;          /* PC13 -> R13 -> D3 */
    gpio.GPIO_Mode  = GPIO_Mode_Out_PP;
    gpio.GPIO_Speed = GPIO_Speed_2MHz;
    GPIO_Init(GPIOC, &gpio);

    Set_USBConfig();
    USB_Init();
    USB_Interrupts_Config();

    while (1)
    {
        poll_input();

        if (--led_loops == 0u)
        {
            led_loops = LED_TOGGLE_LOOPS;
            GPIO_WriteBit(GPIOC, GPIO_Pin_13,
                          (GPIO_ReadOutputDataBit(GPIOC, GPIO_Pin_13) == Bit_SET)
                              ? Bit_RESET : Bit_SET);
        }
        Delay_Us(100);
    }
}
