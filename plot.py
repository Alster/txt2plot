#!/usr/bin/env python3
"""Convert SVG pixel-unit G-code to mm and stream to GRBL plotter."""

import sys
import serial
import time

PORT = '/dev/ttyACM0'
BAUD = 115200


def pen_up(s):
    """Raise pen, wait for confirmation."""
    s.write(b'\x18')       # soft-reset to clear GRBL queue
    time.sleep(1.5)
    s.write(b'$X\n')       # unlock
    time.sleep(0.3)
    s.reset_input_buffer()
    s.write(b'G1 F12000 Z3\n')
    s.write(b'G53 G0 X0 Y0\n')
    s.readline()


def send_gcode(gcode_lines):
    s = serial.Serial(PORT, BAUD, timeout=5)
    time.sleep(2)
    s.reset_input_buffer()

    # Soft-reset and unlock
    s.write(b'\x18')
    time.sleep(1.5)
    s.write(b'$X\n')
    time.sleep(0.3)
    s.reset_input_buffer()

    # GRBL serial buffer = 128 bytes. Keep it full for smooth motion.
    GRBL_BUF = 127
    buf_used  = 0
    sent      = 0
    confirmed = 0
    total     = len(gcode_lines)
    queue     = []   # (bytes_in_cmd,)

    try:
        while confirmed < total:
            # Feed as many lines as fit in GRBL buffer
            while sent < total:
                cmd = (gcode_lines[sent] + '\n').encode()
                if buf_used + len(cmd) > GRBL_BUF:
                    break
                s.write(cmd)
                queue.append(len(cmd))
                buf_used += len(cmd)
                sent += 1

            # Wait for next 'ok'
            resp = s.readline().decode(errors='replace').strip()
            if resp == 'ok' or resp.startswith('error'):
                buf_used -= queue.pop(0)
                confirmed += 1
                if confirmed % 100 == 0:
                    print(f'  {confirmed}/{total}', flush=True)

    except KeyboardInterrupt:
        print('\nInterrupted — raising pen...', flush=True)
        pen_up(s)
        s.close()
        sys.exit(1)

    # Pen up and go home
    for cmd in ['G1 F12000 Z3\n', 'G53 G0 X0 Y0\n']:
        s.write(cmd.encode())
        s.readline()

    s.close()
    print('Done.')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(f'Usage: python3 plot.py <file.gcode>')
        sys.exit(1)

    with open(sys.argv[1]) as f:
        lines = [l.strip() for l in f if l.strip()]

    print(f'Streaming {len(lines)} lines to {PORT} ...')
    send_gcode(lines)
