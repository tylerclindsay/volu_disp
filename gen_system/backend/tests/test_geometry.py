import copy
from pathlib import Path

import numpy as np
import pytest

from volu_gen.geometry import (DisplayConfigError, Geometry, load_display_config,
                               parse_display_config)

PROTOTYPE = Path(__file__).resolve().parents[3] / "config" / "displays" / "prototype.yaml"


def config_dict(**geometry) -> dict:
    """A valid two-node config dict (the prototype's values); geometry keys override."""
    return {
        "version": 1,
        "geometry": {"size": {"x": 10, "y": 12, "z": 8}, "strip_spacing": 50,
                     "pixel_spacing": 50, "slice_spacing": 25,
                     "odd_slice_offset": {"x": 25, "y": 25}, **geometry},
        "timing": {"default_fps": 120},
        "network": {"udp_port": 7777},
        "drive_nodes": [
            {"id": 1, "ip": "192.168.1.101", "section": {"x": [0, 9], "y": [0, 11], "z": [0, 3]}},
            {"id": 2, "ip": "192.168.1.102", "section": {"x": [0, 9], "y": [0, 11], "z": [4, 7]}},
        ],
    }


def geometry_of(d: dict) -> Geometry:
    return Geometry.from_config(parse_display_config(d))


def test_prototype_loads():
    cfg = load_display_config(PROTOTYPE)
    assert cfg.geometry.size == (10, 12, 8)
    assert cfg.default_fps == 120 and cfg.udp_port == 7777
    assert [(n.id, n.ip, n.section.strips, n.section.height) for n in cfg.drive_nodes] == [
        (1, "192.168.1.101", 40, 12), (2, "192.168.1.102", 40, 12)]
    assert Geometry.from_config(cfg).pixel_count == 960


def test_geometry_order_and_positions():
    """i = (z * X + x) * Y + y; odd slices shifted by the offset; y grows downward."""
    g = geometry_of(config_dict())
    X, Y, Z = g.size
    for x, y, z in [(0, 0, 0), (9, 11, 7), (3, 5, 1), (0, 1, 0), (1, 0, 0)]:
        i = (z * X + x) * Y + y
        assert g.index(x, y, z) == i
        assert (g.ix[i], g.iy[i], g.iz[i]) == (x, y, z)
    assert g.positions[g.index(0, 0, 0)].tolist() == [0, 0, 0]
    assert g.positions[g.index(1, 2, 0)].tolist() == [50, 100, 0]
    assert g.positions[g.index(1, 2, 1)].tolist() == [75, 125, 25]
    assert g.positions[g.index(9, 11, 7)].tolist() == [475, 575, 175]
    # Each strip is contiguous, pixel 0 (top) first.
    assert np.all(np.diff(g.positions[:Y, 1]) == 50)
    assert np.array_equal(g.index(g.ix, g.iy, g.iz), np.arange(g.pixel_count))


def test_bcc_neighbor_distances():
    """With the prototype pitches the 8 nearest neighbors are ≈ 43 mm away (docs/hardware.md)."""
    g = geometry_of(config_dict())
    p = g.positions[g.index(4, 5, 4)]
    d = np.sort(np.linalg.norm(g.positions - p, axis=1))[1:]
    assert np.allclose(d[:8], np.sqrt(25**2 + 25**2 + 25**2))
    assert np.allclose(d[8:14], 50)


def test_arrays_are_read_only():
    g = geometry_of(config_dict())
    with pytest.raises(ValueError):
        g.positions[0, 0] = 1


def test_geometry_hash_vector():
    """Fixed test vector: changing it means every existing render goes stale."""
    assert geometry_of(config_dict()).hash == Geometry.from_config(load_display_config(PROTOTYPE)).hash
    assert geometry_of(config_dict()).hash == "c8f51a734795a627"


def test_geometry_hash_tracks_positions_only():
    base = geometry_of(config_dict()).hash
    assert geometry_of(config_dict(strip_spacing=50.0)).hash == base     # 50 == 50.0
    for change in [{"strip_spacing": 51}, {"slice_spacing": 30},
                   {"odd_slice_offset": {"x": 25, "y": 0}},
                   {"size": {"x": 10, "y": 13, "z": 8}}]:
        assert geometry_of(config_dict(**change)).hash != base, change
    rewired = config_dict()
    rewired["drive_nodes"][0]["ip"] = "192.168.1.150"
    rewired["drive_nodes"][0]["section"]["z"] = [0, 4]
    rewired["drive_nodes"][1]["section"]["z"] = [5, 7]
    assert geometry_of(rewired).hash == base


def with_change(path: str, value) -> dict:
    """config_dict() with the value at a dotted path replaced (or deleted if value is ...)."""
    d = copy.deepcopy(config_dict())
    *parents, last = path.split(".")
    node = d
    for key in parents:
        node = node[int(key)] if isinstance(node, list) else node[key]
    if value is ...:
        del node[last]
    else:
        node[last] = value
    return d


@pytest.mark.parametrize("path,value,message", [
    ("version", 2, "not supported"),
    ("geometry.strip_spacing", ..., "missing"),
    ("geometry.strip_spcing", 50, "unknown keys"),
    ("geometry.size.x", 0, "below 1"),
    ("geometry.size.x", 10.5, "whole number"),
    ("geometry.size.y", True, "whole number"),
    ("geometry.pixel_spacing", -5, "> 0"),
    ("geometry.slice_spacing", float("nan"), "finite"),
    ("geometry.odd_slice_offset.y", "25", "number"),
    ("timing.default_fps", 29, "below 30"),
    ("timing.default_fps", 481, "above 480"),
    ("network.udp_port", 70000, "above 65535"),
    ("drive_nodes.0.ip", "192.168.1.300", "ip"),
    ("drive_nodes.1.id", 1, "duplicate id"),
    ("drive_nodes.1.ip", "192.168.1.101", "duplicate ip"),
    ("drive_nodes.0.section.x", [0, 10], "above 9"),
    ("drive_nodes.0.section.z", [3, 0], "min 3 > max 0"),
    ("drive_nodes.0.section.y", [0], "[min, max]"),
])
def test_config_errors(path, value, message):
    with pytest.raises(DisplayConfigError, match=message.replace("[", r"\[").replace("]", r"\]")):
        parse_display_config(with_change(path, value))


def test_empty_drive_nodes():
    d = config_dict()
    d["drive_nodes"] = []
    with pytest.raises(DisplayConfigError, match="non-empty"):
        parse_display_config(d)
