/********************************************************************************
 * settings.c -- per-channel settings page in flash. See settings.h.
 ********************************************************************************/

#include <string.h>

#include "ch32v20x.h"
#include "settings.h"
#include "servo.h"
#include "maestro.h"
#include "flash_layout.h"

#define SETTINGS_MAGIC    0x53455431u     /* "SET1" */

typedef struct
{
    uint16_t min_us;
    uint16_t max_us;
    uint16_t speed;                       /* Maestro units, 0 = unlimited */
    uint16_t accel;
} ChannelSettings_t;

typedef struct
{
    uint32_t          magic;
    uint32_t          crc;                /* of ch[] */
    ChannelSettings_t ch[SERVO_CHANNELS];
} SettingsBody_t;

typedef union
{
    SettingsBody_t s;
    uint32_t       words[SETTINGS_SIZE / 4u];
} SettingsPage_t;

_Static_assert(sizeof(SettingsBody_t) <= SETTINGS_SIZE, "settings page too small");
_Static_assert(sizeof(SettingsPage_t) == SETTINGS_SIZE, "settings page layout");

#define FLASH_PAGE ((const SettingsPage_t *)SETTINGS_FLASH_ADDR)

static SettingsPage_t s_page;

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

static bool page_valid(const SettingsBody_t *b)
{
    return b->magic == SETTINGS_MAGIC &&
           b->crc == crc32((const uint8_t *)b->ch, sizeof b->ch);
}

void Settings_Init(void)
{
    const SettingsBody_t *b = &FLASH_PAGE->s;
    uint8_t ch;

    if (!page_valid(b))
    {
        return;                           /* blank or damaged: defaults stand */
    }
    for (ch = 0; ch < SERVO_CHANNELS; ch++)
    {
        /* A bad pair is skipped rather than trusted; the rest still load. */
        (void)Servo_SetLimitsUs(ch, b->ch[ch].min_us, b->ch[ch].max_us);
        Maestro_SetSpeed(ch, b->ch[ch].speed);
        Maestro_SetAccel(ch, b->ch[ch].accel);
    }
}

static bool write_page(void)
{
    if (FLASH_ROM_ERASE(SETTINGS_FLASH_ADDR, SETTINGS_SIZE) != FLASH_COMPLETE ||
        FLASH_ROM_WRITE(SETTINGS_FLASH_ADDR, s_page.words, SETTINGS_SIZE) != FLASH_COMPLETE)
    {
        return false;
    }
    return memcmp((const void *)SETTINGS_FLASH_ADDR, &s_page, SETTINGS_SIZE) == 0;
}

bool Settings_Save(void)
{
    uint8_t ch;

    memset(&s_page, 0, sizeof s_page);
    for (ch = 0; ch < SERVO_CHANNELS; ch++)
    {
        Servo_GetLimitsUs(ch, &s_page.s.ch[ch].min_us, &s_page.s.ch[ch].max_us);
        s_page.s.ch[ch].speed = Maestro_GetSpeed(ch);
        s_page.s.ch[ch].accel = Maestro_GetAccel(ch);
    }
    s_page.s.magic = SETTINGS_MAGIC;
    s_page.s.crc = crc32((const uint8_t *)s_page.s.ch, sizeof s_page.s.ch);
    return write_page();
}

bool Settings_Reset(void)
{
    uint8_t ch;

    for (ch = 0; ch < SERVO_CHANNELS; ch++)
    {
        (void)Servo_SetLimitsUs(ch, SERVO_US_MIN, SERVO_US_MAX);
        Maestro_SetSpeed(ch, 0u);
        Maestro_SetAccel(ch, 0u);
    }
    /* An invalid page, not an erased one: erased CH32 flash does not read as
       all-ones, so write a page that page_valid() rejects outright. */
    memset(&s_page, 0, sizeof s_page);
    return write_page();
}
