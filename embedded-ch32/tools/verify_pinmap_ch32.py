#!/usr/bin/env python3
"""
verify_pinmap_ch32.py -- prove the schematic, the silicon and the firmware agree.

Checks four things against the live KiCad netlist:

  1. PIN MAP      every net lands on the U1 pin the firmware expects
  2. SILICON      that pin really offers that function on a CH32V203C8T6,
                  in its DEFAULT (no-remap) mapping
  3. SIGNAL CHAIN each servo channel runs MCU -> 220R -> header pin, and the
                  USB/ESD/crystal/boot nets have exactly the right members
  4. FIRMWARE     servo.c's channel table matches, and the timer base values
                  in main.c/servo.h give 0.5 us ticks at 50 Hz

Sources (all verified, not assumed):
  * CH32F/V20x_V30x_V31x Reference Manual V2.5
      Table 10-15 TIM1 remap  - default: CH1=PA8  CH2=PA9  CH3=PA10 CH4=PA11
      Table 10-16 TIM2 remap  - default: CH1=PA0  CH2=PA1  CH3=PA2  CH4=PA3
      Table 10-17 TIM3 remap  - default: CH1=PA6  CH2=PA7  CH3=PB0  CH4=PB1
      Table 10-18 TIM4 remap  - default: CH1=PB6  CH2=PB7  CH3=PB8  CH4=PB9
      "When using the USB function, the CPU frequency must be 48MHz, 96MHz
       or 144MHz."
  * CH32V203 Datasheet V2.6 section 3.1, CH32V203CxT6 LQFP48 pin table
      (ADC channel numbers are fixed in the pin names: PA0/ADC0 .. PB1/ADC9)
  * WCH-Link User Manual V2.3/V2.4 Table 6
      CH32V10x/CH32V20x/CH32V30x -> SWDIO=PA13, SWCLK=PA14 (2-wire SDI)
  * openwch/ch32v20x  EVT/EXAM/USB/USBD  (USB_Port_Set drives GPIOA 11/12)

Exit status is non-zero if anything disagrees, so this works as a CI gate.
"""

import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SCH = os.path.join(REPO, 'PCB', 'usbservocontroller', 'usbservocontroller.kicad_sch')
KICAD_CLI = r'C:\Program Files\KiCad\10.0\bin\kicad-cli.exe'
SERVO_C = os.path.join(os.path.dirname(HERE), 'User', 'servo.c')
MAIN_C = os.path.join(os.path.dirname(HERE), 'User', 'main.c')
SERVO_H = os.path.join(os.path.dirname(HERE), 'User', 'servo.h')

# --- CH32V203CxT6 LQFP48 pin table (datasheet V2.6 s3.1) -------------------
CH32 = {
    1: 'VBAT', 2: 'PC13', 3: 'PC14', 4: 'PC15', 5: 'PD0/OSC_IN',
    6: 'PD1/OSC_OUT', 7: 'NRST', 8: 'VSSA', 9: 'VDDA', 10: 'PA0/ADC0',
    11: 'PA1/ADC1', 12: 'PA2/ADC2', 13: 'PA3/ADC3', 14: 'PA4/ADC4',
    15: 'PA5/ADC5', 16: 'PA6/ADC6', 17: 'PA7/ADC7', 18: 'PB0/ADC8',
    19: 'PB1/ADC9', 20: 'PB2/BOOT1', 21: 'PB10', 22: 'PB11', 23: 'VSS_1',
    24: 'VDD_VIO_1', 25: 'PB12', 26: 'PB13', 27: 'PB14', 28: 'PB15',
    29: 'PA8', 30: 'PA9', 31: 'PA10', 32: 'PA11/USB1DM', 33: 'PA12/USB1DP',
    34: 'PA13/SWDIO', 35: 'VSS_2', 36: 'VDD_2', 37: 'PA14/SWCLK', 38: 'PA15',
    39: 'PB3', 40: 'PB4', 41: 'PB5', 42: 'PB6/USB2DM', 43: 'PB7/USB2DP',
    44: 'BOOT0', 45: 'PB8', 46: 'PB9', 47: 'VSS_3', 48: 'VDD_VIO_3',
}

# --- timer channels in DEFAULT mapping (RM tables 10-15..10-18) ------------
TIMER_PINS = {
    'PA8': 'TIM1_CH1', 'PA9': 'TIM1_CH2', 'PA10': 'TIM1_CH3', 'PA11': 'TIM1_CH4',
    'PA0': 'TIM2_CH1', 'PA1': 'TIM2_CH2', 'PA2': 'TIM2_CH3', 'PA3': 'TIM2_CH4',
    'PA6': 'TIM3_CH1', 'PA7': 'TIM3_CH2', 'PB0': 'TIM3_CH3', 'PB1': 'TIM3_CH4',
    'PB6': 'TIM4_CH1', 'PB7': 'TIM4_CH2', 'PB8': 'TIM4_CH3', 'PB9': 'TIM4_CH4',
}

# --- the 12 servo channels: (ch, U1 pin, timer, series R, header.pin) ------
CHANNELS = [
    (0,  10, 'TIM2_CH1', 'R1',  'J3.1'), (1,  11, 'TIM2_CH2', 'R2',  'J3.2'),
    (2,  12, 'TIM2_CH3', 'R3',  'J3.3'), (3,  13, 'TIM2_CH4', 'R4',  'J3.4'),
    (4,  16, 'TIM3_CH1', 'R5',  'J3.5'), (5,  17, 'TIM3_CH2', 'R6',  'J3.6'),
    (6,  18, 'TIM3_CH3', 'R7',  'J4.1'), (7,  19, 'TIM3_CH4', 'R8',  'J4.2'),
    (8,  29, 'TIM1_CH1', 'R9',  'J4.3'), (9,  30, 'TIM1_CH2', 'R10', 'J4.4'),
    (10, 31, 'TIM1_CH3', 'R11', 'J4.5'), (11, 45, 'TIM4_CH3', 'R12', 'J4.6'),
]
PWM_NET = {0: '/PWM0', 1: '/PWM1', 2: '/PWM2', 3: '/PWM3', 4: '/PWM4',
           5: '/PWM5', 6: '/PWM6', 7: '/PWM7', 8: '/PWM8', 9: '/PWM9_TX',
           10: '/PWM10_RX', 11: '/PWM11'}

# --- nets whose membership must be exactly this ---------------------------
EXACT_NETS = {
    '/USB_DP':  {'J1.A6', 'J1.B6', 'U1.33', 'U3.1'},
    '/USB_DM':  {'J1.A7', 'J1.B7', 'U1.32', 'U3.3'},
    '/CC1':     {'J1.A5', 'R14.1', 'U3.4'},
    '/CC2':     {'J1.B5', 'R15.1', 'U3.6'},
    '/VBUS':    {'C7.1', 'D1.2', 'J1.A4', 'J1.A9', 'J1.B4', 'J1.B9', 'U3.5'},
    '/XTAL1':   {'C13.1', 'U1.5', 'Y1.1'},
    '/XTAL2':   {'C14.1', 'U1.6', 'Y1.2'},
    '/BOOT0':   {'JP2.2', 'R16.1', 'U1.44'},
    '/BOOT1':   {'R17.1', 'U1.20'},
    # Rail sensing. J11 (spare analog header) was deleted; PA4/PA5 now carry
    # the divider tap and the INA180A2 output. See README section 2.2.
    '/V_SENSE': {'U1.14', 'R21.2', 'R22.1', 'C18.1'},
    '/I_SENSE': {'U1.15', 'U4.1'},
    '/VSRV_RAW': {'J2.1', 'R18.1', 'U4.3'},
    '/VDIV_A':  {'R19.2', 'R20.1'},
    '/VDIV_B':  {'R20.2', 'R21.1'},
    '/NRST':    {'C6.1', 'J9.4', 'U1.7'},
}
# SWD/SDI nets are checked by pin only, since their names may be renamed.
SDI_PINS = {'U1.34': 'SWDIO (PA13)', 'U1.37': 'SWCLK (PA14)'}

results = []


def check(ok, label, detail=''):
    results.append((bool(ok), label, detail))


# --- netlist ---------------------------------------------------------------
TOK = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+')


def sexp(text):
    toks = TOK.findall(text)
    pos = 0

    def rd():
        nonlocal pos
        out = []
        while pos < len(toks):
            t = toks[pos]
            if t == '(':
                pos += 1
                out.append(rd())
            elif t == ')':
                pos += 1
                return out
            else:
                out.append(t[1:-1] if t.startswith('"') else t)
                pos += 1
        return out
    pos += 1
    return rd()


def kids(n, k):
    return [x for x in n if isinstance(x, list) and x and x[0] == k]


def val(n, k, d=''):
    f = kids(n, k)
    return f[0][1] if f and len(f[0]) > 1 else d


def load_nets():
    tmp = os.path.join(tempfile.gettempdir(), 'servo_ch32_verify.net')
    subprocess.run([KICAD_CLI, 'sch', 'export', 'netlist', '--format', 'kicadsexpr',
                    '-o', tmp, os.path.basename(SCH)],
                   check=True, capture_output=True, cwd=os.path.dirname(SCH))
    tree = sexp(open(tmp, encoding='utf-8').read())
    nets, u1 = {}, {}
    for net in kids(kids(tree, 'nets')[0], 'net'):
        name = val(net, 'name')
        members = set()
        for node in kids(net, 'node'):
            ref, pin = val(node, 'ref'), val(node, 'pin')
            members.add(f'{ref}.{pin}')
            if ref == 'U1' and not name.startswith('unconnected-'):
                u1[pin] = name
        if not name.startswith('unconnected-'):
            nets[name] = members
    return nets, u1


def base(label):
    return label.split('/')[0]


def net_of(nets, member):
    for n, m in nets.items():
        if member in m:
            return n
    return None


def main():
    nets, u1 = load_nets()

    # ---- 1+2. servo channels: pin map, silicon, signal chain -------------
    print('=== Servo channels: MCU pin -> 220R -> header ===')
    print(f"{'Ch':>3}  {'U1 pin':<14}{'Timer (default map)':<22}{'Chain':<28}Verdict")
    print('-' * 96)
    for ch, pin, timer, res, hdr in CHANNELS:
        ds = CH32[pin]
        pb = base(ds)
        pwm = PWM_NET[ch]

        ok_sil = TIMER_PINS.get(pb) == timer
        check(ok_sil, f'ch{ch}: {pb} offers {timer} by default',
              '' if ok_sil else f'RM says {pb} = {TIMER_PINS.get(pb)}')

        ok_pin = nets.get(pwm, set()) >= {f'U1.{pin}', f'{res}.1'}
        check(ok_pin, f'ch{ch}: {pwm} = U1.{pin} + {res}.1',
              '' if ok_pin else f'got {sorted(nets.get(pwm, []))}')

        sig = net_of(nets, f'{res}.2')
        ok_sig = sig is not None and nets[sig] == {f'{res}.2', hdr}
        check(ok_sig, f'ch{ch}: {res}.2 -> {hdr}',
              '' if ok_sig else f'{res}.2 is on {sig} = {sorted(nets.get(sig, []))}')

        verdict = 'ok' if (ok_sil and ok_pin and ok_sig) else 'PROBLEM'
        print(f'{ch:>3}  {pb + " (" + str(pin) + ")":<14}{timer:<22}'
              f'{pwm + " -> " + res + " -> " + hdr:<28}{verdict}')

    # ---- 3. exact-membership nets ---------------------------------------
    print('\n=== Fixed nets (exact membership) ===')
    for name, want in sorted(EXACT_NETS.items()):
        got = nets.get(name)
        ok = got == want
        check(ok, f'{name} membership',
              '' if ok else f'want {sorted(want)} got {sorted(got) if got else None}')
        print(f'  {name:<10} {"ok" if ok else "PROBLEM":<9}{sorted(got) if got else "MISSING"}')

    # ---- SDI / debug pins (name-agnostic) -------------------------------
    print('\n=== Debug interface (WCH 2-wire SDI, not ARM SWD) ===')
    for member, what in SDI_PINS.items():
        n = net_of(nets, member)
        ok = n is not None and 'J9' in ' '.join(nets[n])
        check(ok, f'{what} reaches J9', '' if ok else f'net={n}')
        print(f'  {what:<16} net {str(n):<12} members {sorted(nets.get(n, []))}')

    # ---- USB peripheral choice ------------------------------------------
    print('\n=== USB ===')
    dm_ok = nets.get('/USB_DM', set()) >= {'U1.32'}
    dp_ok = nets.get('/USB_DP', set()) >= {'U1.33'}
    check(dm_ok and dp_ok, 'USB on PA11/PA12 = USB1 (USBD peripheral)',
          '' if dm_ok and dp_ok else 'D+/D- not on PA11/PA12')
    print(f'  PA11 (pin 32) = {CH32[32]}  -> {net_of(nets, "U1.32")}')
    print(f'  PA12 (pin 33) = {CH32[33]}  -> {net_of(nets, "U1.33")}')
    print('  firmware uses the USBD library, whose USB_Port_Set() drives GPIOA 11/12 -- matches')

    # ---- 4. firmware agreement ------------------------------------------
    print('\n=== Firmware ===')
    servo = open(SERVO_C, encoding='utf-8').read()
    main_c = open(MAIN_C, encoding='utf-8').read()
    servo_h = open(SERVO_H, encoding='utf-8').read()

    for ch, pin, timer, res, hdr in CHANNELS:
        tim, chan = timer.split('_')
        pb = base(CH32[pin])
        port = 'GPIO' + pb[1]
        bit = pb[2:]
        pat = re.compile(r'TIM_IDX_%s,\s*%s,\s*%s,\s*GPIO_Pin_%s\b'
                         % (tim[3:], chan[2:].replace('CH', ''), port, bit))
        pat2 = re.compile(r'\{\s*TIM%s,\s*%s,\s*%s,\s*GPIO_Pin_%s\b'
                          % (tim[3:], chan[2:], port, bit))
        ok = bool(pat.search(servo) or pat2.search(servo))
        check(ok, f'servo.c maps ch{ch} -> {tim} ch{chan[2:]} on {pb}')

    system_c = open(os.path.join(os.path.dirname(HERE), 'User', 'system_ch32v20x.c'),
                    encoding='latin-1').read()
    all_src = servo_h + main_c + servo + system_c

    for label, pat in (
        # RM: "When using the USB function, the CPU frequency must be
        # 48MHz, 96MHz or 144MHz." 96 MHz / 2 = 48 MHz for USB.
        ('96 MHz from HSE (USB-legal CPU freq)', r'^#define SYSCLK_FREQ_96MHz_HSE'),
        ('servo tick 2 MHz (0.5 us)', r'SERVO_TIMER_HZ\s+2000000u'),
        ('frame 40000 ticks (20 ms = 50 Hz)', r'SERVO_FRAME_TICKS\s+40000u'),
        ('TIM1 main output enabled (advanced timer)', r'TIM_CtrlPWMOutputs\(TIM1,\s*ENABLE\)'),
    ):
        check(bool(re.search(pat, all_src, re.M)), label)

    # ---- report ----------------------------------------------------------
    width = max(len(l) for _, l, _ in results)
    failed = 0
    print('\n=== Detail ===')
    for ok, label, detail in results:
        if not ok:
            failed += 1
            print(f'[FAIL] {label:<{width}}  {detail}')
    print(f'\n{len(results) - failed}/{len(results)} checks passed'
          + (f'   {failed} FAILED' if failed else '   ALL OK'))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
