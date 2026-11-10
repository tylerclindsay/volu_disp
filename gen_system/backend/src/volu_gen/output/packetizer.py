"""Frame → PIXELS packets per drive node (docs/protocol.md, v2).

Each node's buffer is cut into full packets of 486 pixels (the last one
shorter), crossing strip boundaries freely: the fewest packets per frame.
Packets live in one preallocated buffer per node and are rewritten in
place each frame, so the hot path does no allocation beyond the gather.
"""

from __future__ import annotations

import numpy as np

from ..mapping import Mapping
from .protocol import (FLAG_SHOW_NOW, FRAME_MOD, HEADER, MAGIC, MAX_PIXELS_PER_PACKET,
                       PIXELS_HEADER, TYPE_PIXELS, VERSION)


class _NodePackets:
    def __init__(self, buffer_index: np.ndarray):
        n = len(buffer_index)
        start = int(buffer_index[0])
        # A node whose pixels are one run of the frame (e.g. sections split by
        # slice, decision 0013) is a slice, not a gather.
        if np.array_equal(buffer_index, np.arange(start, start + n)):
            self.take = slice(start, start + n)
        else:
            self.take = buffer_index
        self.offsets = list(range(0, n, MAX_PIXELS_PER_PACKET))
        self.counts = [min(MAX_PIXELS_PER_PACKET, n - o) for o in self.offsets]
        self.buf = bytearray(PIXELS_HEADER.size * len(self.offsets) + 3 * n)
        self.arr = np.frombuffer(self.buf, np.uint8)
        view = memoryview(self.buf)
        self.starts, self.packets = [], []
        pos = 0
        for o, c in zip(self.offsets, self.counts):
            PIXELS_HEADER.pack_into(self.buf, pos, MAGIC, VERSION, TYPE_PIXELS, 0, 0, o, c)
            self.starts.append(pos)
            self.packets.append(view[pos:pos + PIXELS_HEADER.size + 3 * c])
            pos += PIXELS_HEADER.size + 3 * c


class Packetizer:
    """Builds every node's PIXELS packets for a frame.

        p = Packetizer(mapping)
        for k, pkts in enumerate(p.packetize(frame, frame_no)):
            ...  # send pkts to mapping.nodes[k]

    The returned packets are views into buffers reused on the next call:
    send (or copy) them before packetizing again.
    """

    def __init__(self, mapping: Mapping):
        self.pixel_count = mapping.geometry.pixel_count
        self._nodes = [_NodePackets(n.buffer_index) for n in mapping.nodes]

    def packets_per_frame(self) -> list[int]:
        return [len(n.packets) for n in self._nodes]

    def packetize(self, frame: np.ndarray, frame_no: int, show_now: bool = False,
                  nodes=None) -> list[list[memoryview] | None]:
        """PIXELS packets for one (P, 3) uint8 RGB frame in geometry order,
        per node in mapping order. `nodes` (indices into mapping.nodes)
        limits the work to those nodes; others get None. show_now sets
        SHOW_NOW on each node's last packet (test tools, single node use)."""
        if frame.shape != (self.pixel_count, 3) or frame.dtype != np.uint8:
            raise ValueError(f"frame must be ({self.pixel_count}, 3) uint8, "
                             f"got {frame.shape} {frame.dtype}")
        frame_no %= FRAME_MOD
        out: list[list[memoryview] | None] = []
        for k, n in enumerate(self._nodes):
            if nodes is not None and k not in nodes:
                out.append(None)
                continue
            rgb = frame[n.take]
            last = len(n.packets) - 1
            for j, (pos, o, c) in enumerate(zip(n.starts, n.offsets, n.counts)):
                flags = FLAG_SHOW_NOW if show_now and j == last else 0
                HEADER.pack_into(n.buf, pos, MAGIC, VERSION, TYPE_PIXELS, flags, frame_no)
                d = pos + PIXELS_HEADER.size
                n.arr[d:d + 3 * c] = rgb[o:o + c].ravel()
            out.append(n.packets)
        return out
