import math

import numpy as np
import pytest

from volu_gen.analysis.audio import BASE_SR
from volu_gen.analysis.features import N_BANDS
from volu_gen.analysis.grid import grid_times
from volu_gen.animfile import AnimReader
from volu_gen.generators import (METHODS, PLAN_STREAM, DirectorParams, Music, RenderContext, SegmentInfo,
                                 make_plan, render_anim, render_context, segment_rng)
from volu_gen.generators.context import box_positions
from volu_gen.generators.methods import Strobe
from volu_gen.generators.methods.common import Signal, hit_env, rotation, soft, span
from volu_gen.generators.methods.geometry import SHAPES, distance
from volu_gen.generators.methods.raindrops import FALL
from volu_gen.geometry import Geometry, GeometrySpec

BPM, OFFSET, DUR = 120.0, 0.5, 20.0   # bars of 2 s, beats of 0.5 s; bar 1 at 0.5 s
FIRST = ("galaxy", "geometry", "raindrops", "strobe")


def analysis() -> dict:
    beats, downbeats = grid_times(BPM, OFFSET, DUR)
    return {
        "version": 2, "source": {"duration": DUR, "sha256": "cd" * 32},
        "tempo": {"mode": "grid", "bpm": BPM, "offset": OFFSET, "meter": 4,
                  "beats": beats.tolist(), "downbeats": downbeats.tolist(), "override": None},
        "sections": [{"start": 0.0, "end": 8.5, "start_bar": 1}, {"start": 8.5, "end": DUR, "start_bar": 5}],
        "phrases": [{"start": 0.5, "end": 8.5, "start_bar": 1, "bars": 4, "section": 0, "tags": ["breakdown"]},
                    {"start": 8.5, "end": 16.5, "start_bar": 5, "bars": 4, "section": 1, "tags": ["drop"]}],
        "events": [],
        "bars": {"energy_db": {"mix": [-20.0, -20, -20, -20, 0, 0, 0, 0, -6, -6]}},
        "mood": {"cos": {"dark": 0.3, "calm": 0.1}},
    }


def features() -> dict:
    """Kicks on every beat, hats on the off-beats, envelopes that swell."""
    sr, env_hop, spec_hop = BASE_SR, 147, 441
    n = int(DUR * sr)
    env_t, spec_t = np.arange(n // env_hop + 1) * env_hop / sr, np.arange(n // spec_hop + 1) * spec_hop / sr
    env = np.tile((0.5 + 0.5 * np.sin(env_t * 0.7)).astype(np.float16), (5, 1))
    beats, downbeats = grid_times(BPM, OFFSET, DUR)
    kick = (beats, np.where(np.isin(beats, downbeats), 1.0, 0.4).astype(np.float32))
    hat = (beats[:-1] + 0.25, np.full(len(beats) - 1, 0.7, np.float32))
    return {"version": 3, "sample_rate": sr, "env_hop": env_hop, "spec_hop": spec_hop, "n_fft": 4096,
            "n_samples": n, "lanes": ["mix", "vocals", "drums", "bass", "other"], "classes": ["kick", "hat"],
            "band_edges": np.geomspace(40, 16000, N_BANDS + 1), "env": env,
            "env_ref": np.full(5, 0.5, np.float32), "spec": np.zeros((5, N_BANDS, len(spec_t)), np.float16),
            "width": np.full((5, len(spec_t)), 0.5, np.float16),
            "onsets": {"kick": kick, "hat": hat}, "env_t": env_t, "spec_t": spec_t}


GEO = Geometry.from_spec(GeometrySpec((6, 8, 4), 50.0, 50.0, 25.0, (25.0, 25.0)))


@pytest.fixture
def music() -> Music:
    return Music(analysis(), features())


def method(music, mid, fps=97, start_bar=1, end_bar=5, **over):
    ctx = RenderContext(GEO, music, fps, seed=3, palette=("#ff0000", "#00ff00", "#0000ff"))
    seg = SegmentInfo.from_bars(music, start_bar, end_bar)
    cls = METHODS[mid]
    params = cls.choose_params(seg, music, segment_rng(3, start_bar, PLAN_STREAM))
    return cls(ctx, seg, params.model_copy(update=over) if over else params)


# -- every method
@pytest.mark.parametrize("mid", FIRST)
def test_registered(mid):
    assert METHODS[mid].id == mid and METHODS[mid].version >= 1


@pytest.mark.parametrize("mid", FIRST)
@pytest.mark.parametrize("bars", [(1, 5), (5, 9), (7, 8)])
def test_choose_params(music, mid, bars):
    cls = METHODS[mid]
    seg = SegmentInfo.from_bars(music, *bars)
    a = cls.choose_params(seg, music, segment_rng(1, bars[0], PLAN_STREAM))
    assert a == cls.choose_params(seg, music, segment_rng(1, bars[0], PLAN_STREAM))   # deterministic
    assert cls.Params.model_validate(a.model_dump(mode="json")) == a                  # plain JSON


@pytest.mark.parametrize("mid", FIRST)
@pytest.mark.parametrize("fps", [30, 97, 480])
def test_render_contract(music, mid, fps):
    m = method(music, mid, fps, 5, 7)
    a, b = m.ctx.frame_at(m.seg.start - 1.0), m.ctx.frame_at(m.seg.end + 1.0)   # fades reach past the segment
    f = m.render(a, b)
    assert f.dtype == np.float32 and f.shape == (b - a, GEO.pixel_count, 3)
    assert np.isfinite(f).all() and f.min() >= 0 and f.max() <= 1
    assert f.max() > 0.5                                                          # it draws something


@pytest.mark.parametrize("mid", FIRST)
def test_any_range_renders_the_same(music, mid):
    m = method(music, mid, 97, 5, 9)
    a, b = m.ctx.frame_at(7.5), m.ctx.frame_at(19.0)
    whole = m.render(a, b)
    cuts = [a, a + 1, a + 8, a + 101, a + 400, b]
    assert np.array_equal(np.concatenate([m.render(x, y) for x, y in zip(cuts, cuts[1:])]), whole)
    again = method(music, mid, 97, 5, 9)                                          # same seed and segment
    assert np.array_equal(again.render(a, b), whole)


def test_director_plans_and_renders_them(tmp_path, music):
    plan = make_plan(music, DirectorParams(seed=4))
    assert {s.method for s in plan.segments} <= set(METHODS)
    ctx = render_context(GEO, music, 30, plan)
    meta = render_anim(tmp_path / "x.anim", ctx, plan, "r1")
    with AnimReader(tmp_path / "x.anim") as rd:
        frames = np.concatenate([rd.chunk(k) for k in range(rd.chunk_count)])
    assert meta["frame_count"] == len(frames) == 600 and frames.max() > 128


def test_every_method_is_chosen_somewhere(music):
    seen = {s.method for seed in range(40) for s in make_plan(music, DirectorParams(seed=seed)).segments}
    assert set(FIRST) <= seen                                                     # (other tests register more)


# -- RenderContext
def test_box_and_pitch():
    ctx = RenderContext(GEO, Music(analysis()), 120)
    assert ctx.box.min(axis=0) == pytest.approx([-1, -1, -1]) and ctx.box.max(axis=0) == pytest.approx([1, 1, 1])
    assert ctx.box[0, 1] == pytest.approx(1.0)                                    # pixel 0: the top
    assert ctx.pitch == pytest.approx(2 * 50 / 375)                               # y pitch: 50 mm of 375
    assert box_positions(np.array([[0, 0, 0], [10, 0, 0]], np.float64)).tolist() == [[-1, 0, 0], [1, 0, 0]]


# -- common
def test_hit_env(music):
    t = np.array([0.49, 0.5, 0.6, 1.0, 2.5])
    e = hit_env(music, "kick", t, decay=0.1)
    assert e[0] == pytest.approx(0.4 * np.exp(-4.9))                              # a weak kick at 0 s, decayed
    assert e[1] == pytest.approx(1.0)                                             # the bar-1 kick, strength 1
    assert e[2] == pytest.approx(np.exp(-1.0))
    assert e[3] == pytest.approx(0.4)                                             # a weak kick, on its beat
    assert e[4] == pytest.approx(1.0)                                             # bar 2


def test_hit_env_keeps_a_strong_tail(music):
    m = Music(analysis(), {**features(), "onsets": {"kick": (np.array([1.0, 1.05]), np.array([1.0, 0.1], np.float32)),
                                                     "hat": (np.zeros(0), np.zeros(0, np.float32))}})
    e = hit_env(m, "kick", np.array([1.04, 1.06]), decay=0.2)
    assert e[1] == pytest.approx(np.exp(-0.06 / 0.2))                              # not cut to the weak hit's 0.1


def test_signal():
    s = Signal(np.array([0.0, 1.0, 2.0]), np.array([1.0, 1.0, 3.0]))
    assert s(1.5) == pytest.approx(2.0)
    assert s.integral(np.array([-1.0, 0.0, 1.0, 2.0, 3.0])) == pytest.approx([-1.0, 0.0, 1.0, 3.0, 6.0])


def test_envelope_is_smooth_and_range_free(music):
    seg = SegmentInfo.from_bars(music, 2, 4)
    e = Signal.envelope(music, "bass", seg, tau=0.2)
    t = np.linspace(seg.start, seg.end, 500)
    assert np.abs(np.diff(e(t))).max() < 0.01
    assert e(t) == pytest.approx(music.env("bass", t), abs=0.2)                  # follows the slow swell


def test_soft_and_rotation():
    assert soft(np.array([-1.0, 0.0, 0.5, 1.0, 2.0]), 1.0) == pytest.approx([1, 1, 0.5, 0, 0])
    R = rotation([0, 0, 1], np.array([np.pi / 2]))[0]
    assert R @ np.array([1.0, 0, 0]) == pytest.approx([0, 1, 0])
    R = rotation([1, 2, 3], np.linspace(0, 6, 5))
    assert np.einsum("nij,nkj->nik", R, R) == pytest.approx(np.broadcast_to(np.eye(3), (5, 3, 3)))


# -- strobe
def flashes(f: np.ndarray) -> np.ndarray:
    """Frames where a flash starts."""
    on = f.max(axis=(1, 2)) > 0
    return np.flatnonzero(on & ~np.concatenate([[False], on[:-1]]))


@pytest.mark.parametrize("fps", [30, 97, 120])
def test_strobe_lands_every_flash_on_the_grid(music, fps):
    m = method(music, "strobe", fps, 5, 9, per_beat=2, duty=0.05, pattern="all")
    a, b = m.ctx.frame_at(8.5), m.ctx.frame_at(16.5)
    starts = flashes(m.render(a, b)) + a
    expected = [math.ceil((8.5 + 0.25 * k) * fps - 1e-9) for k in range(32)]     # first frame at or after each
    assert list(starts) == expected                                              # 8 per bar, none missed


def test_strobe_rate_limit(music):
    m = method(music, "strobe", 30, 5, 9, per_beat=4, duty=0.3, pattern="all")   # 8 flashes/s asked
    ctx = RenderContext(GEO, music, 30, seed=3, strobe_max_rate=3.0)
    m = Strobe(ctx, m.seg, m.params)
    a, b = ctx.frame_at(8.5), ctx.frame_at(16.5)
    assert len(flashes(m.render(a, b))) == 16                                    # halved twice: 2 flashes/s


def test_strobe_patterns(music):
    m = method(music, "strobe", 120, 5, 9, per_beat=1, pattern="slices", follow=0.0, white=0.0)
    f = m.render(m.ctx.frame_at(8.5), m.ctx.frame_at(9.5)).max(-1)
    lit = [set(np.unique(m.ctx.parity[f[i] > 0])) for i in (0, 60)]               # beats 1 and 2
    assert lit[0] != lit[1] and all(len(s) == 1 for s in lit)
    m = method(music, "strobe", 120, 5, 9, per_beat=1, pattern="chase")
    f = m.render(m.ctx.frame_at(8.5), m.ctx.frame_at(10.5))
    assert [int(np.unique(m.ctx.iz[f[i].max(-1) > 0])[0]) for i in range(0, 240, 60)] == [0, 1, 2, 3]


# -- raindrops
def test_raindrops_land_on_hats(music):
    m = method(music, "raindrops", 97, 1, 5, hats=1.0, kicks=False, ambient=0.0)
    t0, t1 = span(m.seg)
    assert t0 < m.seg.start - 1 and t1 > m.seg.end + 1                            # covers the fade margins
    assert list(m.drops[:, 0]) == list(music.onsets("hat", t0, t1 + FALL)[0])
    big = method(music, "raindrops", 97, 1, 5, hats=0.0, kicks=True, ambient=0.0)
    assert list(big.drops[:, 0]) == list(music.onsets("kick", t0, t1 + FALL, min_strength=0.5)[0])
    assert (big.drops[:, 3] > m.drops[:, 3].max()).all()                          # kicks drop big drops


def test_raindrops_sea_follows_the_bass(music):
    m = method(music, "raindrops", 97, 1, 9, waves=0.0, hats=0.0, kicks=False, ambient=0.0, swell=0.5)
    t = np.array([2.0, 6.7])                                                      # bass ≈ 2 × env_ref, then ≈ 0
    lv = m._level(t)
    assert lv[0] > lv[1] + 0.5


# -- geometry
@pytest.mark.parametrize("shape,on", [
    ("sphere", [0.0, 1.0, 0.0]), ("cube", [0.62, 0.62, 0.0]), ("octahedron", [0.0, 0.0, 1.0]),
    ("torus", [0.0, 0.25, 0.75]), ("rings", [0.0, 0.0, 1.0])])
def test_shape_distances(shape, on):
    q = np.array([on, [0.0, 0.0, 0.0], [3.0, 3.0, 3.0]], np.float32)
    d = distance(shape, q)
    assert d[0] == pytest.approx(0.0, abs=1e-6) and d[2] > 1.0
    assert shape in SHAPES


def test_geometry_morphs_onto_the_beat(music):
    m = method(music, "geometry", 120, 5, 9, shapes=["sphere", "cube"], swap_beats=2, morph=0.5)
    t = np.array([8.5, 9.0, 9.25, 9.375, 9.5, 10.5])     # beats of 0.5 s: swaps on 9.5 s and 10.5 s
    a, b, w = m._shapes(t)
    assert list(b) == [0, 0, 1, 1, 1, 0] and list(a) == [1, 1, 0, 0, 0, 1]
    assert w == pytest.approx([1.0, 1.0, 0.0, 0.5, 1.0, 1.0])                     # morph starts half a beat early


def test_geometry_kick_pulse(music):
    m = method(music, "geometry", 120, 5, 9, pulse=0.5, wander=0.0, spin=0.0, shapes=["sphere"], glow=0.0)
    f = m.render(m.ctx.frame_at(8.5) - 6, m.ctx.frame_at(8.5) + 1)
    assert not np.array_equal(f[0], f[-1])                                        # the kick grows the sphere
