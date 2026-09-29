/********************************************************************************
 * script.h -- stored servo scripts that run without a PC.
 *
 * A script is up to SCRIPT_MAX_INSTR fixed-size instructions kept in the last
 * 4 KB of flash (flash_layout.h), behind a header with a CRC. The host uploads
 * it one instruction per command line into a RAM staging buffer, then commits
 * it to flash in one go (tools/servoscript.py compiles the text form).
 *
 * Instructions (op, ch, a, b):
 *   END                        stop; servos keep holding position
 *   MOVE   ch  a=us    b=ms    ramp ch to a over b ms; does NOT wait, so
 *                              several MOVEs run together (b=0: jump)
 *   WAIT           b=ms        pause
 *   SYNC                       wait until every MOVE has arrived
 *   OFF    ch (255 = all)      stop driving ch
 *   LOOP       a=idx  b=n      jump back to idx; the body runs n times in
 *                              total (n = 0: forever)
 *   JUMP       a=idx           jump to idx
 *   WAITIN ch  a=thr  b=cond   wait until analog input ch is above thr
 *                              (cond 0) or below thr (cond 1); ch 0-7
 *   IFIN   ch  a=thr  b=cond   run the next instruction only if the input
 *                              condition holds, otherwise skip it
 *
 * Runs from the main loop, timed off TIM2's 2 MHz servo counter. A latched
 * rail fault or the X command stops it. With the autostart flag it starts at
 * power-up, which is how the board runs a script with no PC attached (logic
 * power then comes from the servo supply through JP1).
 ********************************************************************************/
#ifndef __SCRIPT_H
#define __SCRIPT_H

#include <stdint.h>
#include <stdbool.h>

#define SCRIPT_MAX_INSTR    254u         /* 16 B header + 254 x 8 B = 2 KB */
#define SCRIPT_FLAG_AUTORUN 0x01u
#define SCRIPT_CH_ALL       255u

typedef enum
{
    SOP_END = 0, SOP_MOVE, SOP_WAIT, SOP_SYNC, SOP_OFF,
    SOP_LOOP, SOP_JUMP, SOP_WAITIN, SOP_IFIN,
    SOP_COUNT
} ScriptOp_t;

typedef struct
{
    uint8_t  op;
    uint8_t  ch;
    uint16_t a;
    uint32_t b;
} ScriptInstr_t;

typedef enum { SCRIPT_IDLE = 0, SCRIPT_RUNNING, SCRIPT_DONE, SCRIPT_FAULTED } ScriptState_t;

/** @brief Power-up: start the stored script if it is valid and set to autorun. */
void Script_Init(void);

/** @brief Main-loop hook: advances ramps and executes instructions. */
void Script_Task(void);

/** @brief Stop a running script. Servos keep their last position. */
void Script_Stop(void);

/** @brief True while a script is executing. */
bool Script_Running(void);

/**
  * @brief Handle a 'Q' command line (the text after the Q) and write the reply,
  *        including its line ending, into out.
  */
void Script_Command(const char *args, char *out, uint32_t out_len);

#endif /* __SCRIPT_H */
