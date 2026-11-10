"""Display config (config/displays/*.yaml): loading and schema checks.
Units are mm, indices 0-based. Axes: x across a slice, y vertical (y = 0 at
the top), z depth (slice). Checks that span drive nodes (overlap, coverage,
strips per node, FPS fit) are in `volu_gen.mapping`.
"""

from __future__ import annotations

import ipaddress
import math
from dataclasses import dataclass
from pathlib import Path

import yaml

CONFIG_VERSION = 1
MIN_FPS, MAX_FPS = 30, 480  # display FPS range (decision 0010)


class DisplayConfigError(ValueError):
    """The display config is malformed or of an unsupported version."""


@dataclass(frozen=True)
class GeometrySpec:
    """The `geometry` block: display size in pixels and pitches in mm."""
    size: tuple[int, int, int]           # (x, y, z): strips across, pixels per strip, slices
    strip_spacing: float                 # x pitch
    pixel_spacing: float                 # y pitch
    slice_spacing: float                 # z pitch
    odd_slice_offset: tuple[float, float]  # (x, y) shift of every odd slice


@dataclass(frozen=True)
class Section:
    """A drive node's box of the display: inclusive index ranges per axis."""
    x: tuple[int, int]
    y: tuple[int, int]
    z: tuple[int, int]

    @property
    def width(self) -> int:
        return self.x[1] - self.x[0] + 1

    @property
    def height(self) -> int:
        """Pixels per strip on this node (the firmware's LEDS_PER_STRIP)."""
        return self.y[1] - self.y[0] + 1

    @property
    def depth(self) -> int:
        return self.z[1] - self.z[0] + 1

    @property
    def strips(self) -> int:
        """One strip per (x, z) column."""
        return self.width * self.depth


@dataclass(frozen=True)
class DriveNode:
    id: int
    ip: str
    section: Section


@dataclass(frozen=True)
class DisplayConfig:
    geometry: GeometrySpec
    default_fps: int
    udp_port: int
    drive_nodes: tuple[DriveNode, ...]
    path: Path | None = None


def load_display_config(path: Path) -> DisplayConfig:
    """Read and check a display config file. Raises DisplayConfigError."""
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise DisplayConfigError(f"{path}: invalid YAML: {e}") from e
    return parse_display_config(data, path)


def parse_display_config(data, path: Path | None = None) -> DisplayConfig:
    """Check a parsed config (dict) and build a DisplayConfig. Unknown keys
    are errors, so a typo never silently falls back to a default."""
    where = str(path) if path else "display config"
    _keys(data, where, ("version", "geometry", "timing", "network", "drive_nodes"))
    if data["version"] != CONFIG_VERSION:
        raise DisplayConfigError(f"{where}: version {data['version']!r} is not supported "
                                 f"(expected {CONFIG_VERSION})")

    g = data["geometry"]
    _keys(g, "geometry", ("size", "strip_spacing", "pixel_spacing", "slice_spacing",
                          "odd_slice_offset"))
    _keys(g["size"], "geometry.size", ("x", "y", "z"))
    size = tuple(_int(g["size"][a], f"geometry.size.{a}", lo=1) for a in "xyz")
    _keys(g["odd_slice_offset"], "geometry.odd_slice_offset", ("x", "y"))
    geometry = GeometrySpec(
        size=size,
        strip_spacing=_num(g["strip_spacing"], "geometry.strip_spacing", positive=True),
        pixel_spacing=_num(g["pixel_spacing"], "geometry.pixel_spacing", positive=True),
        slice_spacing=_num(g["slice_spacing"], "geometry.slice_spacing", positive=True),
        odd_slice_offset=tuple(_num(g["odd_slice_offset"][a], f"geometry.odd_slice_offset.{a}")
                               for a in "xy"),
    )

    _keys(data["timing"], "timing", ("default_fps",))
    default_fps = _int(data["timing"]["default_fps"], "timing.default_fps", lo=MIN_FPS, hi=MAX_FPS)
    _keys(data["network"], "network", ("udp_port",))
    udp_port = _int(data["network"]["udp_port"], "network.udp_port", lo=1, hi=65535)

    nodes_raw = data["drive_nodes"]
    if not isinstance(nodes_raw, list) or not nodes_raw:
        raise DisplayConfigError("drive_nodes: must be a non-empty list")
    nodes = tuple(_drive_node(n, k, size) for k, n in enumerate(nodes_raw))
    for attr in ("id", "ip"):
        values = [getattr(n, attr) for n in nodes]
        dups = sorted({v for v in values if values.count(v) > 1}, key=str)
        if dups:
            raise DisplayConfigError(f"drive_nodes: duplicate {attr} {dups}")
    return DisplayConfig(geometry, default_fps, udp_port, nodes, Path(path) if path else None)


def _drive_node(n, k: int, size: tuple[int, int, int]) -> DriveNode:
    where = f"drive_nodes[{k}]"
    _keys(n, where, ("id", "ip", "section"))
    node_id = _int(n["id"], f"{where}.id", lo=0)
    try:
        ip = str(ipaddress.IPv4Address(n["ip"]))
    except (ipaddress.AddressValueError, ValueError) as e:
        raise DisplayConfigError(f"{where}.ip: {e}") from e
    s = n["section"]
    _keys(s, f"{where}.section", ("x", "y", "z"))
    ranges = []
    for axis, extent in zip("xyz", size):
        r = s[axis]
        w = f"{where}.section.{axis}"
        if not isinstance(r, list) or len(r) != 2:
            raise DisplayConfigError(f"{w}: must be [min, max]")
        lo, hi = (_int(v, w, lo=0, hi=extent - 1) for v in r)
        if lo > hi:
            raise DisplayConfigError(f"{w}: min {lo} > max {hi}")
        ranges.append((lo, hi))
    return DriveNode(node_id, ip, Section(*ranges))


def _keys(d, where: str, required: tuple[str, ...]):
    """d must be a mapping with exactly the required keys."""
    if not isinstance(d, dict):
        raise DisplayConfigError(f"{where}: must be a mapping")
    missing = [k for k in required if k not in d]
    unknown = [k for k in d if k not in required]
    if missing:
        raise DisplayConfigError(f"{where}: missing {missing}")
    if unknown:
        raise DisplayConfigError(f"{where}: unknown keys {unknown}")


def _int(v, where: str, lo: int | None = None, hi: int | None = None) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise DisplayConfigError(f"{where}: must be a whole number, got {v!r}")
    if lo is not None and v < lo:
        raise DisplayConfigError(f"{where}: {v} is below {lo}")
    if hi is not None and v > hi:
        raise DisplayConfigError(f"{where}: {v} is above {hi}")
    return v


def _num(v, where: str, positive: bool = False) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise DisplayConfigError(f"{where}: must be a finite number, got {v!r}")
    if positive and not v > 0:
        raise DisplayConfigError(f"{where}: must be > 0, got {v}")
    return float(v)
