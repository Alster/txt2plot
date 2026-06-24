#!/usr/bin/env python3
"""
set_zero.py — Set the plotter work origin (G54 zero point).

On start the pen moves to the current work (0, 0) so you can see where
the zero is physically. Adjust paper or carriage as needed, then:

  s      — save current machine position as the new work origin (0, 0)
  c      — draw calibration corners on A4 to verify alignment
  Ctrl-C — exit without saving
"""
import re
import sys
import time
import serial

PORT  = '/dev/ttyACM0'
BAUD  = 115200
F_JOG = 12000  # mm/min

# Calibration corner marks: L-shaped ticks at all four A4 corners.
# Each corner is drawn 10 mm inset from the edge, 8 mm long.
CALIBRATION_GCODE = [
    # Bottom-left
    'G0 X18 Y10',
    'G1 F12000 Z5',
    'G1 F14000 X10 Y10',
    'G1 F14000 X10 Y18',
    'G1 F12000 Z3',
    # Top-left
    'G0 X18 Y287',
    'G1 F12000 Z5',
    'G1 F14000 X10 Y287',
    'G1 F14000 X10 Y279',
    'G1 F12000 Z3',
    # Bottom-right
    'G0 X192 Y10',
    'G1 F12000 Z5',
    'G1 F14000 X200 Y10',
    'G1 F14000 X200 Y18',
    'G1 F12000 Z3',
    # Top-right
    'G0 X192 Y287',
    'G1 F12000 Z5',
    'G1 F14000 X200 Y287',
    'G1 F14000 X200 Y279',
    'G1 F12000 Z3',
    # Return home
    'G0 X0 Y0',
]


def readline(s):
    return s.readline().decode(errors='replace').strip()


def send(s, cmd):
    s.write((cmd + '\n').encode())
    return readline(s)


def get_g54(s):
    """Read current G54 work offset from GRBL ($# command)."""
    s.write(b'$#\n')
    ox, oy = 0.0, 0.0
    for _ in range(20):
        line = readline(s)
        m = re.search(r'\[G54:([0-9.-]+),([0-9.-]+)', line)
        if m:
            ox, oy = float(m.group(1)), float(m.group(2))
        if line == 'ok':
            break
    return ox, oy


def get_mpos(s, g54):
    """Return machine position (MPos) regardless of $10 reporting mode."""
    s.write(b'?')
    st = readline(s)

    m = re.search(r'MPos:([0-9.-]+),([0-9.-]+)', st)
    if m:
        return float(m.group(1)), float(m.group(2))

    m = re.search(r'WPos:([0-9.-]+),([0-9.-]+)', st)
    if m:
        return float(m.group(1)) + g54[0], float(m.group(2)) + g54[1]

    return None, None


def wait_idle(s, timeout=30):
    for _ in range(int(timeout / 0.1)):
        s.write(b'?')
        st = readline(s)
        if st.startswith('<Idle'):
            return
        time.sleep(0.1)


def draw_calibration(s):
    """Send calibration corner G-code and wait for completion."""
    print('  Drawing calibration corners ...')
    for cmd in CALIBRATION_GCODE:
        send(s, cmd)
    wait_idle(s, timeout=60)
    print('  Done.')


def main():
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

    g54 = get_g54(s)

    # Move to current work (0, 0) so the user can see where zero is
    print('Moving to current work origin (0, 0) ...')
    send(s, 'G1 F12000 Z3')       # pen up
    send(s, 'G0 X0 Y0')           # go to work zero
    wait_idle(s)

    mx, my = get_mpos(s, g54)
    print(f'Pen is at work (0, 0)  →  MPos X={mx:.2f}  Y={my:.2f}')
    print()
    print('Adjust paper or carriage position as needed, then:')
    print('  s      — save current position as new work origin')
    print('  c      — draw calibration corners to verify alignment')
    print('  Ctrl-C — exit without saving')
    print()

    while True:
        try:
            cmd = input('> ').strip().lower()
        except (KeyboardInterrupt, EOFError):
            print('\nCancelled — nothing was saved.')
            send(s, 'G1 F12000 Z3')   # pen up before exit
            s.close()
            sys.exit(0)

        if cmd == 's':
            send(s, 'G10 L20 P1 X0 Y0')
            send(s, 'G1 F12000 Z3')   # pen up before exit
            g54  = get_g54(s)
            mx, my = get_mpos(s, g54)
            print(f'Saved. New G54 offset: X={g54[0]:.3f}  Y={g54[1]:.3f}')
            print(f'Current MPos: X={mx:.2f}  Y={my:.2f}  (should match G54)')
            s.close()
            sys.exit(0)

        if cmd == 'c':
            draw_calibration(s)
            g54 = get_g54(s)
            continue

        print('  s — save    c — calibrate    Ctrl-C — cancel')


if __name__ == '__main__':
    main()
