"""UDP sender: per frame, every node's PIXELS packets, then one SYNC
broadcast (docs/protocol.md). Keeps the protocol frame counter, which is
separate from the animation frame index and runs on across seeks, track
changes and test patterns.
"""

from __future__ import annotations

import ipaddress
import socket
from collections import Counter

import numpy as np

from ..mapping import Mapping
from .packetizer import Packetizer
from .protocol import FRAME_MOD, encode_sync

SEND_BUFFER = 4 << 20   # room for a whole frame of a large display


def subnet_broadcast(mapping: Mapping, prefix: int = 24) -> str:
    """Broadcast address of the /prefix subnet that holds every drive node.
    Raises ValueError if the nodes span several."""
    nets = {ipaddress.IPv4Interface(f"{n.ip}/{prefix}").network for n in mapping.nodes}
    if len(nets) != 1:
        raise ValueError(f"drive nodes are on several /{prefix} subnets: "
                         f"{sorted(map(str, nets))}")
    return str(nets.pop().broadcast_address)


class Sender:
    """Sends frames to the drive nodes.

    targets       (host, port) per node, in mapping order
    sync_targets  where each SYNC goes: the subnet broadcast on hardware,
                  or every target on loopback (no broadcast there)
    bind_ip       local address of the display network interface (Windows
                  needs it to pick the interface for broadcasts)
    stats         Counter: frames, packets, send_errors
    """

    def __init__(self, mapping: Mapping, targets: list[tuple[str, int]],
                 sync_targets: list[tuple[str, int]], bind_ip: str | None = None):
        if len(targets) != len(mapping.nodes):
            raise ValueError(f"{len(targets)} targets for {len(mapping.nodes)} nodes")
        self.mapping = mapping
        self.targets = targets
        self.sync_targets = sync_targets
        self.packetizer = Packetizer(mapping)
        self.frame_no = 0
        self.stats: Counter = Counter()
        self.last_error: OSError | None = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SEND_BUFFER)
        if bind_ip:
            self.sock.bind((bind_ip, 0))

    @classmethod
    def for_display(cls, mapping: Mapping, bind_ip: str | None = None,
                    broadcast: str | None = None) -> Sender:
        """Hardware: each node's configured IP and the config's UDP port."""
        port = mapping.config.udp_port
        targets = [(n.ip, port) for n in mapping.nodes]
        return cls(mapping, targets, [(broadcast or subnet_broadcast(mapping), port)], bind_ip)

    @classmethod
    def loopback(cls, mapping: Mapping, base_port: int) -> Sender:
        """Fake nodes on this machine: node k at 127.0.0.1:base_port + k
        (tools/fake_node.py). SYNC is sent to each, since loopback has no
        broadcast."""
        targets = [("127.0.0.1", base_port + k) for k in range(len(mapping.nodes))]
        return cls(mapping, targets, targets)

    def send_frame(self, frame: np.ndarray, nodes=None, show_now: bool = False):
        """Send one (P, 3) uint8 RGB frame in geometry order, then SYNC, and
        advance the frame counter. `nodes` (indices into mapping.nodes)
        limits PIXELS to those nodes (the online set). Send errors are
        counted, never raised: one bad frame must not stop playback."""
        frame_no = self.frame_no
        self.frame_no = (frame_no + 1) % FRAME_MOD
        for target, packets in zip(self.targets,
                                   self.packetizer.packetize(frame, frame_no, show_now, nodes)):
            for pkt in packets or ():
                self._send(pkt, target)
        if not show_now:
            sync = encode_sync(frame_no)
            for target in self.sync_targets:
                self._send(sync, target)
        self.stats["frames"] += 1

    def _send(self, pkt, target):
        try:
            self.sock.sendto(pkt, target)
            self.stats["packets"] += 1
        except OSError as e:
            self.stats["send_errors"] += 1
            self.last_error = e

    def close(self):
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
