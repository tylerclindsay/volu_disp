"""A simulated drive node: the v2 receive rules of docs/protocol.md → Frames
(double buffer, late drop, latch on SYNC or SHOW_NOW), plus counters for
anything the host should never do. Used by tests and tools/fake_node.py.
"""

from __future__ import annotations

from collections import Counter

import numpy as np

from .protocol import FRAME_MOD, LATE_WINDOW, Pixels, ProtocolError, Sync, decode


class NodeSim:
    """One drive node with `buffer_len` pixels (NUM_STRIPS × LEDS_PER_STRIP).

    back / front  (buffer_len, 3) uint8: what PIXELS write / what is shown
    stats         Counter: packets, pixels, syncs, shown, incomplete, late,
                  clipped (pixels past the buffer end), bad:<reason>, ignored
    """

    def __init__(self, buffer_len: int):
        self.buffer_len = buffer_len
        self.back = np.zeros((buffer_len, 3), np.uint8)
        self.front = np.zeros((buffer_len, 3), np.uint8)
        self.current: int | None = None   # newest frame number seen
        self.shown_frame: int | None = None
        self._written = np.zeros(buffer_len, bool)  # pixels written for `current`
        self.stats: Counter = Counter()
        self.last_error: str | None = None

    def receive(self, packet: bytes):
        """Handle one UDP payload."""
        self.stats["packets"] += 1
        try:
            msg = decode(packet)
        except ProtocolError as e:
            self.stats[f"bad:{e.reason}"] += 1
            self.last_error = str(e)
            return
        if msg is None:
            self.stats["ignored"] += 1
        elif isinstance(msg, Pixels):
            self._pixels(msg)
        else:
            self._sync(msg)

    def _pixels(self, p: Pixels):
        if self.current is not None:
            age = (self.current - p.frame) % FRAME_MOD
            if 1 <= age <= LATE_WINDOW:
                self.stats["late"] += 1
                return
        if p.frame != self.current:
            self.current = p.frame
            self._written[:] = False
        end = min(p.offset + p.count, self.buffer_len)
        n = max(0, end - p.offset)
        if n < p.count:
            self.stats["clipped"] += p.count - n
        if n:
            rgb = np.frombuffer(p.rgb, np.uint8).reshape(-1, 3)
            self.back[p.offset:end] = rgb[:n]
            self._written[p.offset:end] = True
            self.stats["pixels"] += n
        if p.show_now:
            self._latch(p.frame)

    def _sync(self, s: Sync):
        self.stats["syncs"] += 1
        self._latch(s.frame)

    def _latch(self, frame: int):
        self.front[:] = self.back
        self.shown_frame = frame
        self.stats["shown"] += 1
        if frame != self.current or not self._written.all():
            self.stats["incomplete"] += 1
