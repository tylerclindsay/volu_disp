#!/usr/bin/env python3
"""Print drive node logs and stats lines.

Once the LED output starts, a drive node's serial port is gone (UART0's
pins carry LED data), so the firmware sends its log and stats lines as
UDP text to port 7778 (STATS_UDP_PORT in firmware/drive_node/src/config.h)
on the host that last sent it a valid protocol packet. Run this on that
host:

    python tools/node_stats.py
"""
import argparse
import socket
import time


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=7778, help="UDP port (default 7778)")
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", args.port))
    print(f"listening on UDP {args.port}")
    while True:
        data, (ip, _) = sock.recvfrom(2048)
        text = data.decode("utf-8", "replace").rstrip()
        print(f"{time.strftime('%H:%M:%S')} {ip:15} {text}", flush=True)


if __name__ == "__main__":
    main()
