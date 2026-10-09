#!/usr/bin/env python3
"""Stream a test rainbow to the ESP32 LED receiver over UDP.

Usage:
    python udp_rainbow_sender.py 192.168.1.101 --strips 2 --leds 10 --fps 60
"""
import argparse
import colorsys
import socket
import struct
import time

MAGIC = 0xA7
FLAG_SHOW = 0x01
HEADER = struct.Struct("<BBBHH")  # magic, flags, strip, start, count (7 bytes)
MAX_PIXELS_PER_PACKET = 488       # (1472 - 7) // 3


def build_frame(strips_rgb):
    """strips_rgb: list of bytes objects (R,G,B per pixel), one per strip.
    Returns a list of packets; the last one carries the SHOW flag."""
    packets = []
    for strip, data in enumerate(strips_rgb):
        total = len(data) // 3
        for start in range(0, total, MAX_PIXELS_PER_PACKET):
            count = min(MAX_PIXELS_PER_PACKET, total - start)
            chunk = data[start * 3:(start + count) * 3]
            packets.append([strip, start, count, chunk])

    out = []
    for i, (strip, start, count, chunk) in enumerate(packets):
        flags = FLAG_SHOW if i == len(packets) - 1 else 0
        out.append(HEADER.pack(MAGIC, flags, strip, start, count) + chunk)
    return out


def rainbow(strip, leds, t):
    buf = bytearray()
    for i in range(leds):
        h = (i / leds + t * 0.2 + strip * 0.1) % 1.0
        r, g, b = colorsys.hsv_to_rgb(h, 1.0, 1.0)
        buf += bytes((int(r * 255), int(g * 255), int(b * 255)))
    return bytes(buf)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("--port", type=int, default=7777)
    ap.add_argument("--strips", type=int, default=2)
    ap.add_argument("--leds", type=int, default=10)
    ap.add_argument("--fps", type=float, default=60)
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest = (args.host, args.port)
    period = 1.0 / args.fps
    t0 = time.time()
    next_t = t0

    print(f"Sending to {dest} - Ctrl+C to stop")
    try:
        while True:
            t = time.time() - t0
            frame = [rainbow(s, args.leds, t) for s in range(args.strips)]
            for pkt in build_frame(frame):
                sock.sendto(pkt, dest)
            next_t += period
            delay = next_t - time.time()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.time()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()