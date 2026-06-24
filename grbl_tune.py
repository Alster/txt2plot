#!/usr/bin/env python3
"""
grbl_tune.py — Read and optionally update GRBL speed/acceleration settings.

Connects to the plotter, reads $$, shows the parameters that affect
print speed, and lets you enter new values interactively.

Relevant parameters:
  $110  Max X speed (mm/min)
  $111  Max Y speed (mm/min)
  $112  Max Z speed (mm/min)
  $120  X acceleration (mm/s²)
  $121  Y acceleration (mm/s²)
  $122  Z acceleration (mm/s²)

Usage:
  python grbl_tune.py           # read-only, just show current values
  python grbl_tune.py --set     # read then prompt for new values
"""

import re
import sys
import time
import serial

PORT = '/dev/ttyACM0'
BAUD = 115200

PARAMS = {
    110: 'Max X speed     (mm/min)',
    111: 'Max Y speed     (mm/min)',
    112: 'Max Z speed     (mm/min)',
    120: 'X acceleration  (mm/s²)',
    121: 'Y acceleration  (mm/s²)',
    122: 'Z acceleration  (mm/s²)',
}


def readline(s):
    return s.readline().decode(errors='replace').strip()


def send(s, cmd):
    s.write((cmd + '\n').encode())
    return readline(s)


def read_settings(s):
    """Read $$ output and return {param_num: value} for params we care about."""
    s.write(b'$$\n')
    values = {}
    for _ in range(100):
        line = readline(s)
        m = re.match(r'\$(\d+)=([0-9.]+)', line)
        if m:
            n = int(m.group(1))
            if n in PARAMS:
                values[n] = float(m.group(2))
        if line == 'ok':
            break
    return values


def main():
    set_mode = '--set' in sys.argv

    print(f'Connecting to {PORT} ...')
    s = serial.Serial(PORT, BAUD, timeout=3)
    time.sleep(2)
    s.reset_input_buffer()

    s.write(b'\x18')   # soft reset
    time.sleep(1.5)
    s.reset_input_buffer()
    send(s, '$X')      # unlock
    time.sleep(0.2)
    s.reset_input_buffer()

    print('\nCurrent GRBL speed/acceleration settings:\n')
    values = read_settings(s)
    for n, label in PARAMS.items():
        val = values.get(n, '?')
        print(f'  ${n:<4} {label}  =  {val}')

    if not set_mode:
        print('\nRun with --set to update values.')
        s.close()
        return

    print('\nEnter new value (or press Enter to keep current):\n')
    for n, label in PARAMS.items():
        cur = values.get(n, '?')
        try:
            raw = input(f'  ${n} {label} [{cur}]: ').strip()
        except (KeyboardInterrupt, EOFError):
            print('\nCancelled.')
            s.close()
            sys.exit(0)
        if not raw:
            continue
        try:
            new_val = float(raw)
        except ValueError:
            print(f'    Skipped (invalid number)')
            continue
        resp = send(s, f'${n}={new_val}')
        print(f'    → ${n}={new_val}  ({resp})')

    print('\nUpdated. New values:')
    values = read_settings(s)
    for n, label in PARAMS.items():
        val = values.get(n, '?')
        print(f'  ${n:<4} {label}  =  {val}')

    s.close()


if __name__ == '__main__':
    main()
