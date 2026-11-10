"""Pixel → (drive node, strip, pixel) mapping and its validation
(docs/hardware.md → Drive node mapping; decision 0013).

Each drive node owns a box (`section`) of the display. Strips inside a node
are numbered slice by slice, then across: `strip = (z - z0) * width + (x - x0)`;
pixel 0 is the top of the strip (`led = y - y0`). The node's pixel buffer
(protocol.md → PIXELS) has pixel `led` of strip `s` at `s * height + led`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ..geometry import MAX_FPS, MIN_FPS, DisplayConfig, DriveNode, Geometry

MAX_STRIPS_PER_NODE = 64       # two edge nodes × 32 strips
PIXEL_US = 30.0                # WS2811 time per pixel per strip
RESET_US = 50.0                # WS2811 reset (latch) time
MAX_BUFFER_PIXELS = 1 << 16    # PIXELS offset is a u16


class MappingError(ValueError):
    """The display config's drive nodes don't map the display validly.
    `problems` lists every problem found, for the UI's validation results."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def max_strip_leds(fps: int) -> int:
    """Longest strip that a show fits in one frame at fps."""
    return math.floor((1e6 / fps - RESET_US) / PIXEL_US)


def max_fps_for(leds_per_strip: int) -> int:
    """Highest whole FPS whose frame time fits a strip of this length."""
    return math.floor(1e6 / (leds_per_strip * PIXEL_US + RESET_US))


def fps_problems(fps: int, leds_per_strip: int) -> list[str]:
    """Why fps is not a valid display FPS for this longest strip (empty if it is)."""
    if isinstance(fps, bool) or not isinstance(fps, int) or not MIN_FPS <= fps <= MAX_FPS:
        return [f"display FPS {fps!r} must be a whole number from {MIN_FPS} to {MAX_FPS}"]
    if leds_per_strip > max_strip_leds(fps):
        return [f"display FPS {fps}: the longest strip ({leds_per_strip} pixels) needs "
                f"≤ {max_fps_for(leds_per_strip)} FPS"]
    return []


@dataclass(frozen=True, eq=False)
class NodeMap:
    """One drive node's part of the mapping.

    buffer_index  (strips * leds_per_strip,) int64: the geometry index of
                  each pixel in the node's buffer, so `frame[buffer_index]`
                  is the node's buffer.
    """
    node: DriveNode
    strips: int
    leds_per_strip: int
    buffer_index: np.ndarray

    @property
    def id(self) -> int:
        return self.node.id

    @property
    def ip(self) -> str:
        return self.node.ip

    @property
    def buffer_len(self) -> int:
        return len(self.buffer_index)


class Mapping:
    """The validated pixel mapping of a display config. Building it raises
    MappingError unless the sections don't overlap, cover the whole display,
    each hold ≤ 64 strips and fit a protocol buffer, and the config's default
    FPS fits the longest strip.

    nodes      NodeMap per drive node, in config order
    node_of    (P,) int32 position in `nodes` of each pixel's node
    strip_of   (P,) int32 strip within the node
    led_of     (P,) int32 pixel along the strip (0 = top)
    offset_of  (P,) int32 index in the node's pixel buffer
    """

    def __init__(self, config: DisplayConfig, geometry: Geometry | None = None):
        geometry = geometry or Geometry.from_config(config)
        if geometry.size != config.geometry.size:
            raise ValueError(f"geometry size {geometry.size} != config size {config.geometry.size}")
        X, Y, Z = geometry.size
        P = geometry.pixel_count
        grid = np.arange(P).reshape(Z, X, Y)     # geometry order (decision 0013)
        owner = np.full((Z, X, Y), -1, np.int32)
        problems: list[str] = []
        nodes: list[NodeMap] = []
        for k, n in enumerate(config.drive_nodes):
            s = n.section
            box = (slice(s.z[0], s.z[1] + 1), slice(s.x[0], s.x[1] + 1), slice(s.y[0], s.y[1] + 1))
            overlap = np.unique(owner[box][owner[box] >= 0])
            for j in overlap:
                problems.append(f"drive node {n.id} overlaps drive node "
                                f"{config.drive_nodes[j].id}")
            owner[box] = k
            if s.strips > MAX_STRIPS_PER_NODE:
                problems.append(f"drive node {n.id} has {s.strips} strips "
                                f"(max {MAX_STRIPS_PER_NODE})")
            if s.strips * s.height > MAX_BUFFER_PIXELS:
                problems.append(f"drive node {n.id} has {s.strips * s.height} pixels "
                                f"(max {MAX_BUFFER_PIXELS} per node)")
            # z, x, y order inside the box is the strip/led order of the buffer.
            nodes.append(NodeMap(n, s.strips, s.height, grid[box].ravel()))
        uncovered = int((owner < 0).sum())
        if uncovered:
            problems.append(f"{uncovered} of {P} pixels belong to no drive node")
        self.longest_strip = max(n.leds_per_strip for n in nodes)
        problems += [f"timing.default_fps: {p}"
                     for p in fps_problems(config.default_fps, self.longest_strip)]
        if problems:
            raise MappingError(problems)

        self.config = config
        self.geometry = geometry
        self.nodes = tuple(nodes)
        self.node_of = np.empty(P, np.int32)
        self.offset_of = np.empty(P, np.int32)
        self.strip_of = np.empty(P, np.int32)
        self.led_of = np.empty(P, np.int32)
        for k, nm in enumerate(self.nodes):
            offsets = np.arange(nm.buffer_len, dtype=np.int32)
            self.node_of[nm.buffer_index] = k
            self.offset_of[nm.buffer_index] = offsets
            self.strip_of[nm.buffer_index] = offsets // nm.leds_per_strip
            self.led_of[nm.buffer_index] = offsets % nm.leds_per_strip
        for a in (self.node_of, self.offset_of, self.strip_of, self.led_of, *(n.buffer_index for n in nodes)):
            a.setflags(write=False)

    @property
    def max_fps(self) -> int:
        """Highest display FPS this display can run at."""
        return min(MAX_FPS, max_fps_for(self.longest_strip))

    def check_fps(self, fps: int):
        """Raise MappingError if fps is not a valid display FPS for this display."""
        problems = fps_problems(fps, self.longest_strip)
        if problems:
            raise MappingError(problems)
