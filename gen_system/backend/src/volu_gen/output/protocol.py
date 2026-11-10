"""Wire protocol v2, host → drive node (docs/protocol.md). The one place
its constants and byte layout are defined on the host.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = 0xA7
VERSION = 2
TYPE_PIXELS = 1
TYPE_SYNC = 2
TYPE_STATUS = 3        # reserved
FLAG_SHOW_NOW = 0x01   # PIXELS only

DEFAULT_PORT = 7777
MAX_PAYLOAD = 1472     # UDP payload that fits a 1500-byte MTU
FRAME_MOD = 1 << 32    # the frame field is a u32

HEADER = struct.Struct("<BBBBI")   # magic, version, type, flags, frame (8 B)
PIXELS_HEADER = struct.Struct("<BBBBIHH")  # + offset, count (12 B)
MAX_PIXELS_PER_PACKET = (MAX_PAYLOAD - PIXELS_HEADER.size) // 3   # 486
LATE_WINDOW = 8        # PIXELS 1–8 frames older than the newest are dropped


class ProtocolError(ValueError):
    """A packet that a v2 node would drop, or that the host must never send.
    `reason` is a short code for counting (e.g. "magic", "length")."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Pixels:
    frame: int
    offset: int
    rgb: bytes          # 3 * count bytes
    show_now: bool = False

    @property
    def count(self) -> int:
        return len(self.rgb) // 3


@dataclass(frozen=True)
class Sync:
    frame: int


def encode_pixels(frame: int, offset: int, rgb: bytes, show_now: bool = False) -> bytes:
    """One PIXELS packet; rgb is R,G,B per pixel, ≤ 486 pixels."""
    if len(rgb) % 3 or not 0 < len(rgb) // 3 <= MAX_PIXELS_PER_PACKET:
        raise ProtocolError("count", f"PIXELS needs 1–{MAX_PIXELS_PER_PACKET} whole pixels, "
                            f"got {len(rgb)} bytes")
    flags = FLAG_SHOW_NOW if show_now else 0
    return PIXELS_HEADER.pack(MAGIC, VERSION, TYPE_PIXELS, flags, frame % FRAME_MOD,
                              offset, len(rgb) // 3) + bytes(rgb)


def encode_sync(frame: int) -> bytes:
    return HEADER.pack(MAGIC, VERSION, TYPE_SYNC, 0, frame % FRAME_MOD)


def decode(packet: bytes) -> Pixels | Sync | None:
    """Decode one packet as a node would. Returns None for a valid header
    of an unknown or reserved type (nodes ignore those); raises
    ProtocolError for anything a node drops or the spec forbids."""
    if len(packet) < HEADER.size:
        raise ProtocolError("length", f"short packet ({len(packet)} bytes)")
    if len(packet) > MAX_PAYLOAD:
        raise ProtocolError("length", f"packet of {len(packet)} bytes exceeds {MAX_PAYLOAD}")
    magic, version, ptype, flags, frame = HEADER.unpack_from(packet)
    if magic != MAGIC:
        raise ProtocolError("magic", f"bad magic 0x{magic:02X}")
    if version != VERSION:
        raise ProtocolError("version", f"unsupported version {version}")
    if ptype == TYPE_PIXELS:
        if flags & ~FLAG_SHOW_NOW:
            raise ProtocolError("flags", f"PIXELS with reserved flags 0x{flags:02X}")
        if len(packet) < PIXELS_HEADER.size:
            raise ProtocolError("length", "PIXELS shorter than its header")
        offset, count = struct.unpack_from("<HH", packet, HEADER.size)
        if not 0 < count <= MAX_PIXELS_PER_PACKET:
            raise ProtocolError("count", f"PIXELS count {count} outside 1–{MAX_PIXELS_PER_PACKET}")
        if len(packet) != PIXELS_HEADER.size + 3 * count:
            raise ProtocolError("length", f"PIXELS of {count} pixels is {len(packet)} bytes, "
                                f"expected {PIXELS_HEADER.size + 3 * count}")
        return Pixels(frame, offset, bytes(packet[PIXELS_HEADER.size:]),
                      bool(flags & FLAG_SHOW_NOW))
    if ptype == TYPE_SYNC:
        if flags:
            raise ProtocolError("flags", f"SYNC with flags 0x{flags:02X}")
        if len(packet) != HEADER.size:
            raise ProtocolError("length", f"SYNC of {len(packet)} bytes, expected {HEADER.size}")
        return Sync(frame)
    return None
