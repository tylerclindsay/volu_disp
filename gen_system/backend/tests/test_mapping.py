import copy

import numpy as np
import pytest

from test_geometry import PROTOTYPE, config_dict
from volu_gen.geometry import Geometry, load_display_config, parse_display_config
from volu_gen.mapping import (Mapping, MappingError, fps_problems, max_fps_for,
                              max_strip_leds)


def mapping_of(d: dict) -> Mapping:
    return Mapping(parse_display_config(d))


def with_sections(*sections, size=None, fps=120) -> dict:
    """config_dict() with one drive node per (x, y, z) range triple."""
    d = copy.deepcopy(config_dict())
    if size:
        d["geometry"]["size"] = dict(zip("xyz", size))
    d["timing"]["default_fps"] = fps
    d["drive_nodes"] = [{"id": k + 1, "ip": f"192.168.1.{101 + k}",
                         "section": {"x": list(x), "y": list(y), "z": list(z)}}
                        for k, (x, y, z) in enumerate(sections)]
    return d


def test_prototype_mapping():
    """Prototype: node 1 owns z 0–3, node 2 z 4–7; with geometry order each
    node's buffer is one contiguous half of the frame."""
    m = Mapping(load_display_config(PROTOTYPE))
    assert [(n.id, n.strips, n.leds_per_strip, n.buffer_len) for n in m.nodes] == [
        (1, 40, 12, 480), (2, 40, 12, 480)]
    assert np.array_equal(m.nodes[0].buffer_index, np.arange(480))
    assert np.array_equal(m.nodes[1].buffer_index, np.arange(480, 960))
    assert m.longest_strip == 12 and m.max_fps == 480


def check_consistent(m: Mapping):
    """Every pixel is in exactly one node buffer, at the place the inverse arrays say,
    with strip = (z - z0) * width + (x - x0) and led = y - y0."""
    g = m.geometry
    seen = np.concatenate([n.buffer_index for n in m.nodes])
    assert np.array_equal(np.sort(seen), np.arange(g.pixel_count))
    for k, n in enumerate(m.nodes):
        s = n.node.section
        i = n.buffer_index
        assert np.all(m.node_of[i] == k)
        assert np.array_equal(m.offset_of[i], np.arange(n.buffer_len))
        assert np.array_equal(m.strip_of[i], (g.iz[i] - s.z[0]) * s.width + g.ix[i] - s.x[0])
        assert np.array_equal(m.led_of[i], g.iy[i] - s.y[0])
        assert np.array_equal(m.offset_of[i], m.strip_of[i] * n.leds_per_strip + m.led_of[i])


@pytest.mark.parametrize("sections", [
    # prototype split
    [((0, 9), (0, 11), (0, 3)), ((0, 9), (0, 11), (4, 7))],
    # split across x, uneven
    [((0, 2), (0, 11), (0, 7)), ((3, 9), (0, 11), (0, 7))],
    # stacked vertically: each column is two strips, the lower one's pixel 0 at y = 6
    [((0, 9), (0, 5), (0, 3)), ((0, 9), (6, 11), (0, 3)), ((0, 9), (0, 11), (4, 7))],
])
def test_mapping_is_consistent(sections):
    check_consistent(mapping_of(with_sections(*sections)))


def test_strip_numbering_example():
    """A node owning x 3–9, z 0–7: pixel (x=5, y=4, z=2) is strip (2-0)*7 + (5-3) = 16, led 4."""
    m = mapping_of(with_sections(((0, 2), (0, 11), (0, 7)), ((3, 9), (0, 11), (0, 7))))
    i = m.geometry.index(5, 4, 2)
    assert (m.node_of[i], m.strip_of[i], m.led_of[i], m.offset_of[i]) == (1, 16, 4, 16 * 12 + 4)
    # Lower of two stacked strips: its pixel 0 is the top of its section (y = 3).
    m2 = mapping_of(with_sections(((0, 9), (0, 2), (0, 3)), ((0, 9), (3, 11), (0, 3)),
                                  ((0, 9), (0, 11), (4, 7))))
    j = m2.geometry.index(5, 3, 2)
    assert (m2.node_of[j], m2.strip_of[j], m2.led_of[j]) == (1, 2 * 10 + 5, 0)
    assert m2.nodes[1].leds_per_strip == 9 and m2.longest_strip == 12


def test_overlap_and_gap():
    with pytest.raises(MappingError) as e:
        mapping_of(with_sections(((0, 9), (0, 11), (0, 4)), ((0, 9), (0, 11), (4, 6))))
    assert e.value.problems == ["drive node 2 overlaps drive node 1",
                                "120 of 960 pixels belong to no drive node"]


def test_too_many_strips():
    with pytest.raises(MappingError, match="drive node 1 has 70 strips"):
        mapping_of(with_sections(((0, 9), (0, 11), (0, 6)), ((0, 9), (0, 11), (7, 7))))


def test_buffer_limit():
    """64 strips × 1,100 pixels overflows the u16 PIXELS offset."""
    d = with_sections(((0, 7), (0, 1099), (0, 7)), size=(8, 1100, 8), fps=30)
    with pytest.raises(MappingError, match="70400 pixels"):
        mapping_of(d)


def test_default_fps_must_fit_longest_strip():
    tall = with_sections(((0, 9), (0, 299), (0, 3)), ((0, 9), (0, 299), (4, 7)), size=(10, 300, 8))
    with pytest.raises(MappingError, match="longest strip \\(300 pixels\\) needs ≤ 110 FPS"):
        mapping_of(tall)
    tall["timing"]["default_fps"] = 110
    m = mapping_of(tall)
    assert m.max_fps == 110
    m.check_fps(30)
    with pytest.raises(MappingError):
        m.check_fps(111)


@pytest.mark.parametrize("fps", [29, 481, 97.0, True])
def test_fps_range(fps):
    assert fps_problems(fps, 12)


def test_strip_limits_match_docs():
    """docs/hardware.md: ~1,100 / ~270 / ~65 pixels at 30 / 120 / 480 FPS."""
    assert [max_strip_leds(f) for f in (30, 60, 120, 240, 480)] == [1109, 553, 276, 137, 67]
    for leds in (12, 67, 276, 1109):
        assert max_strip_leds(max_fps_for(leds)) >= leds
        assert max_strip_leds(max_fps_for(leds) + 1) < leds


def test_geometry_must_match_config():
    other = Geometry.from_config(parse_display_config(
        with_sections(((0, 9), (0, 10), (0, 7)), size=(10, 11, 8))))
    with pytest.raises(ValueError, match="size"):
        Mapping(load_display_config(PROTOTYPE), other)
