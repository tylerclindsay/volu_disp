#!/usr/bin/env python3
"""Fake drive nodes: receive protocol v2 on UDP, apply the node's receive
rules (volu_gen.output.NodeSim) and print per-node stats once a second.

Run from gen_system/backend (it imports volu_gen):

    # every node of a display on this machine, node k at 127.0.0.1:9000+k
    uv run python ../../tools/fake_node.py ../../config/displays/prototype.yaml --base-port 9000
    uv run python -m volu_gen.output ../../config/displays/prototype.yaml --loopback 9000

    # one node on the display network, in place of real node 2
    uv run python ../../tools/fake_node.py ../../config/displays/prototype.yaml --node 2
"""
import argparse
import select
import socket
import time
from collections import Counter

from volu_gen.geometry import load_display_config
from volu_gen.mapping import Mapping
from volu_gen.output import NodeSim

ISSUES = ("incomplete", "late", "clipped", "ignored")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", help="display config (config/displays/*.yaml)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--base-port", type=int, help="loopback: node k listens on 127.0.0.1:base+k")
    mode.add_argument("--node", type=int, help="act as this drive node id on 0.0.0.0:<udp_port>")
    args = ap.parse_args()

    mapping = Mapping(load_display_config(args.config))
    if args.node is not None:
        ks = [k for k, n in enumerate(mapping.nodes) if n.id == args.node]
        if not ks:
            ap.error(f"no drive node {args.node} in {args.config}")
        binds = {ks[0]: ("0.0.0.0", mapping.config.udp_port)}
    else:
        binds = {k: ("127.0.0.1", args.base_port + k) for k in range(len(mapping.nodes))}

    socks, sims = {}, {}
    for k, addr in binds.items():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        s.bind(addr)
        socks[s] = k
        sims[k] = NodeSim(mapping.nodes[k].buffer_len)
        n = mapping.nodes[k]
        print(f"node {n.id}: {addr[0]}:{addr[1]}, {n.strips} strips x {n.leds_per_strip} "
              f"= {n.buffer_len} pixels")

    last = {k: Counter() for k in sims}
    t_last = time.perf_counter()
    try:
        while True:
            ready, _, _ = select.select(list(socks), [], [], 0.1)
            for s in ready:
                try:
                    pkt = s.recv(65535)
                except ConnectionResetError:   # Windows: ICMP from an earlier send
                    continue
                sims[socks[s]].receive(pkt)
            now = time.perf_counter()
            if now - t_last >= 1.0:
                dt, t_last = now - t_last, now
                for k, sim in sims.items():
                    d = sim.stats - last[k]
                    last[k] = sim.stats.copy()
                    bad = sum(v for key, v in d.items() if key.startswith("bad:"))
                    issues = " ".join(f"{key}={d[key]}" for key in ISSUES if d[key])
                    line = (f"node {mapping.nodes[k].id}: {d['shown'] / dt:6.1f} fps "
                            f"{d['packets'] / dt:7.0f} pkt/s  frame {sim.shown_frame}")
                    if bad:
                        line += f"  BAD={bad} ({sim.last_error})"
                    print(line + (f"  {issues}" if issues else ""))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
