#!/usr/bin/env python3
"""Stream a test rainbow to one drive node over UDP, protocol v2
(docs/protocol.md). Standalone: no gen_system install needed.

Usage:
    python tools/udp_rainbow_sender.py 192.168.1.101 --strips 2 --leds 10 --fps 60

By default the last PIXELS packet of each frame carries SHOW_NOW, so no
broadcast is needed. --sync ADDR sends a SYNC there instead (e.g. the
subnet broadcast 192.168.1.255), as gen_system does. For whole displays
use `python -m volu_gen.output <display config>` from gen_system/backend.
"""
import argparse
import colorsys
import socket
import struct
import time

MAGIC = 0xA7
VERSION = 2
TYPE_PIXELS = 1
TYPE_SYNC = 2
FLAG_SHOW_NOW = 0x01
HEADER = struct.Struct("<BBBBI")          # magic, version, type, flags, frame (8 bytes)
PIXELS_HEADER = struct.Struct("<BBBBIHH")  # + offset, count (12 bytes)
MAX_PIXELS_PER_PACKET = 486               # (1472 - 12) // 3


def build_frame(buf: bytes, frame: int, show_now: bool) -> list[bytes]:
    """buf: the node's flat pixel buffer, R,G,B per pixel, strip after strip.
    Returns full PIXELS packets; with show_now the last one latches."""
    total = len(buf) // 3
    out = []
    for offset in range(0, total, MAX_PIXELS_PER_PACKET):
        count = min(MAX_PIXELS_PER_PACKET, total - offset)
        last = offset + count == total
        flags = FLAG_SHOW_NOW if show_now and last else 0
        out.append(PIXELS_HEADER.pack(MAGIC, VERSION, TYPE_PIXELS, flags, frame, offset, count)
                   + buf[offset * 3:(offset + count) * 3])
    return out


def rainbow(strips: int, leds: int, t: float) -> bytes:
    buf = bytearray()
    for s in range(strips):
        for i in range(leds):
            h = (i / leds + t * 0.2 + s * 0.1) % 1.0
            r, g, b = colorsys.hsv_to_rgb(h, 1.0, 1.0)
            buf += bytes((int(r * 255), int(g * 255), int(b * 255)))
    return bytes(buf)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("--port", type=int, default=7777)
    ap.add_argument("--strips", type=int, default=2)
    ap.add_argument("--leds", type=int, default=10, help="pixels per strip (LEDS_PER_STRIP)")
    ap.add_argument("--fps", type=float, default=60)
    ap.add_argument("--sync", metavar="ADDR", help="send SYNC to ADDR instead of using SHOW_NOW")
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    dest = (args.host, args.port)
    period = 1.0 / args.fps
    t0 = time.time()
    next_t = t0
    frame = 0

    print(f"Sending to {dest} - Ctrl+C to stop")
    try:
        while True:
            t = time.time() - t0
            buf = rainbow(args.strips, args.leds, t)
            for pkt in build_frame(buf, frame, show_now=not args.sync):
                sock.sendto(pkt, dest)
            if args.sync:
                sock.sendto(HEADER.pack(MAGIC, VERSION, TYPE_SYNC, 0, frame), (args.sync, args.port))
            frame = (frame + 1) % (1 << 32)
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
