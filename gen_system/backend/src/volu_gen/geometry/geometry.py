"""Pixel positions and geometry order (decision 0013).

Geometry order is strip by strip, top to bottom: slices (z) outermost, then
strips across a slice (x), then pixels down the strip (y):
`i = (z * X + x) * Y + y`. Positions are in mm in the config's axes, so y
grows downward; odd slices are shifted by `odd_slice_offset`.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

import numpy as np

from .config import DisplayConfig, GeometrySpec

HASH_TAG = b"volu-geometry-1\0"  # bump if the hashed bytes change
HASH_CHARS = 16


def geometry_hash(positions: np.ndarray) -> str:
    """Hash of pixel positions in geometry order: sha256 over a tag, the
    pixel count (u32 LE) and the (P, 3) positions as float64 LE; first 16
    hex chars. Drive nodes are not included: rewiring needs no re-render."""
    pos = np.ascontiguousarray(positions, dtype="<f8")
    h = hashlib.sha256(HASH_TAG + struct.pack("<I", len(pos)) + pos.tobytes())
    return h.hexdigest()[:HASH_CHARS]


@dataclass(frozen=True, eq=False)
class Geometry:
    """Every pixel of the display in geometry order. Arrays are read-only.

    positions  (P, 3) float64 mm (x, y, z)
    ix, iy, iz (P,) int32 indices on each axis
    """
    size: tuple[int, int, int]
    positions: np.ndarray
    ix: np.ndarray
    iy: np.ndarray
    iz: np.ndarray
    hash: str

    @classmethod
    def from_config(cls, config: DisplayConfig) -> Geometry:
        return cls.from_spec(config.geometry)

    @classmethod
    def from_spec(cls, spec: GeometrySpec) -> Geometry:
        X, Y, Z = spec.size
        iz, ix, iy = (a.ravel().astype(np.int32)
                      for a in np.meshgrid(np.arange(Z), np.arange(X), np.arange(Y), indexing="ij"))
        odd = (iz % 2).astype(np.float64)
        ox, oy = spec.odd_slice_offset
        positions = np.stack([ix * spec.strip_spacing + odd * ox,
                              iy * spec.pixel_spacing + odd * oy,
                              iz * spec.slice_spacing], axis=1)
        for a in (positions, ix, iy, iz):
            a.setflags(write=False)
        return cls(spec.size, positions, ix, iy, iz, geometry_hash(positions))

    @property
    def pixel_count(self) -> int:
        return len(self.positions)

    def index(self, x, y, z):
        """Geometry index of the pixel at indices (x, y, z); works on arrays."""
        X, Y, _ = self.size
        return (np.asarray(z) * X + x) * Y + y
