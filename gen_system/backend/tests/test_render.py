import numpy as np
import pytest
from pydantic import ValidationError

from volu_gen.analysis.grid import grid_times
from volu_gen.animfile import AnimReader
from volu_gen.animfile import tmp_path as anim_tmp_path
from volu_gen.generators import (DIRECTOR_VERSION, GENERATOR_ID, DirectorInfo, DirectorParams, Method,
                                 MethodParams, Music, Plan, RenderCancelled, Renderer, Segment, StalePlan,
                                 Transition, render_anim, render_context, to_uint8)
from volu_gen.geometry import Geometry, GeometrySpec

BPM, OFFSET, DUR = 120.0, 0.5, 20.0   # bars of 2 s, beats of 0.5 s; bar 1 at 0.5 s, bar 5 at 8.5 s
SHA = "ab" * 32


def analysis() -> dict:
    beats, downbeats = grid_times(BPM, OFFSET, DUR)
    return {
        "version": 2, "source": {"duration": DUR, "sha256": SHA},
        "tempo": {"mode": "grid", "bpm": BPM, "offset": OFFSET, "meter": 4,
                  "beats": beats.tolist(), "downbeats": downbeats.tolist(), "override": None},
        "sections": [{"start": 0.0, "end": DUR, "start_bar": 1}],
        "phrases": [{"start": 0.5, "end": 16.5, "start_bar": 1, "bars": 8, "section": 0, "tags": []}],
        "events": [], "bars": {"energy_db": {"mix": [-5.0] * 10}},
    }


GEO = Geometry.from_spec(GeometrySpec((2, 3, 2), 50.0, 50.0, 100.0, (25.0, 25.0)))


class Level(MethodParams):
    level: float = 1.0


def solid(method_id: str, channel: int) -> type[Method]:
    """`level` in one channel, everywhere; counts instances and calls."""
    class Solid(Method):
        id = method_id
        version = 1
        Params = Level
        built: list = []
        calls: list = []

        def __init__(self, ctx, seg, params):
            super().__init__(ctx, seg, params)
            type(self).built.append(seg)

        def render(self, start, stop):
            type(self).calls.append((start, stop))
            out = np.zeros((stop - start, self.ctx.pixel_count, 3), np.float32)
            out[..., channel] = self.params.level
            return out

    Solid.built, Solid.calls = [], []
    return Solid


class Clock(Method):
    """Each pixel's value depends on its frame time and position."""
    id = "clock"
    version = 1
    Params = Level

    def render(self, start, stop):
        t = self.ctx.times(start, stop)[:, None, None]
        x = self.ctx.pos[None, :, :]
        return (0.5 + 0.5 * np.sin(t * 3.1 + x * 2.0)).astype(np.float32) * self.params.level


@pytest.fixture
def methods():
    return {m.id: m for m in (solid("red", 0), solid("blue", 2), Clock)}


@pytest.fixture
def music() -> Music:
    return Music(analysis())


def plan(*segs, **params) -> Plan:
    """segs: (method, start_bar, end_bar, transition beats or 0 for a cut[, overrides])."""
    out = []
    for method, a, b, beats, *over in segs:
        tr = Transition(kind="crossfade", beats=beats) if beats else Transition()
        out.append(Segment(start_bar=a, end_bar=b, method=method, method_version=1,
                           params={"level": 1.0}, overrides=over[0] if over else {}, transition=tr))
    return Plan(director=DirectorInfo(version=DIRECTOR_VERSION, params=DirectorParams(**params).model_dump(mode="json")),
                palette=["#ff0000", "#0000ff"], segments=out)


def renderer(music, methods, p: Plan, fps: int = 120) -> Renderer:
    return Renderer(render_context(GEO, music, fps, p), p, methods)


# -- blending
def test_cut_on_the_downbeat(music, methods):
    r = renderer(music, methods, plan(("red", 1, 5, 0), ("blue", 5, 11, 0)), fps=97)
    f = r.linear(0, r.frame_count)
    first_blue = 825                               # 8.5 s × 97 = 824.5: the first frame at or after it
    assert (f[:first_blue, :, 0] == 1).all() and (f[:first_blue, :, 2] == 0).all()
    assert (f[first_blue:, :, 2] == 1).all() and (f[first_blue:, :, 0] == 0).all()
    assert r.frame_count == 1940


def test_crossfade_is_centered_and_sums_to_one(music, methods):
    r = renderer(music, methods, plan(("red", 1, 5, 0), ("blue", 5, 11, 2.0)))   # 2 beats: 8.0–9.0 s
    f = r.linear(0, r.frame_count)
    red, blue = f[:, 0, 0], f[:, 0, 2]
    assert red + blue == pytest.approx(np.ones(r.frame_count), abs=1e-6)
    assert (red[:960 + 1] == 1).all() and (red[1080:] == 0).all()   # 960 = 8.0 s, 1080 = 9.0 s
    assert red[1020] == pytest.approx(0.5)                          # the downbeat, 8.5 s
    assert (np.diff(red) <= 0).all()
    assert np.diff(red[960:1081]).max() < 0 and np.abs(np.diff(red)).max() < 0.02   # smooth


def test_long_fades_are_clipped_and_never_overlap(music, methods):
    p = plan(("red", 1, 2, 0), ("blue", 2, 3, 16.0), ("red", 3, 4, 16.0), ("blue", 4, 11, 16.0))
    r = renderer(music, methods, p)
    w = np.stack([r.weights(i, r.ctx.times(0, r.frame_count)) for i in range(4)])
    assert (w >= 0).all() and (w <= 1).all()
    assert w.sum(axis=0) == pytest.approx(np.ones(r.frame_count), abs=1e-6)
    # segment 1 (bar 2, 2.5–4.5 s) fades over half of itself at each end: 1 only at its middle
    assert w[1].max() == pytest.approx(1.0) and int((w[1] == 1).sum()) <= 1


def test_any_range_renders_the_same(music, methods):
    p = plan(("clock", 1, 3, 0), ("red", 3, 5, 4.0), ("clock", 5, 11, 1.0, {"level": 0.5}))
    whole = renderer(music, methods, p, fps=97).render(0, 1940)
    for n in (1, 7, 97, 500):
        r = renderer(music, methods, p, fps=97)
        assert np.array_equal(np.concatenate(list(r.chunks(n))), whole)
    r = renderer(music, methods, p, fps=97)
    assert np.array_equal(r.render(380, 420), whole[380:420])


def test_methods_render_only_their_frames(music, methods):
    red, blue = methods["red"], methods["blue"]
    r = renderer(music, methods, plan(("red", 1, 5, 0), ("blue", 5, 11, 2.0)))
    list(r.chunks(120))
    assert red.calls[0][0] == 0 and red.calls[-1][1] == 1080        # through the end of the fade
    assert blue.calls[0][0] == 961 and blue.calls[-1][1] == r.frame_count
    assert all(b - a <= 120 for a, b in red.calls + blue.calls)
    assert len(red.built) == len(blue.built) == 1 and not r._live   # built once, dropped when done


def test_segment_info_has_the_render_times(music, methods):
    r = renderer(music, methods, plan(("red", 1, 5, 0), ("blue", 5, 12, 0)))   # bar 12 is past the end
    r.render(0, r.frame_count)
    (a,), (b,) = methods["red"].built, methods["blue"].built
    assert (a.start, a.end, a.start_bar) == (0.0, 8.5, 1)           # covers the lead-in
    assert (b.start, b.end, b.end_bar) == (8.5, DUR, 12)


# -- output
def test_to_uint8():
    v = np.array([-0.1, 0.0, 0.3 / 255, 0.7 / 255, 0.5, 1.0, 1.2], np.float32)
    assert list(to_uint8(v)) == [0, 0, 0, 1, 128, 255, 255]


def test_intensity_and_overrides(music, methods):
    r = renderer(music, methods, plan(("red", 1, 5, 0), ("blue", 5, 11, 0, {"level": 0.5}), intensity=0.5))
    f = r.render(0, r.frame_count)
    assert f[0, 0, 0] == 128 and f[-1, 0, 2] == 64                  # 0.5 × 255; 0.5 × 0.5 × 255


def test_render_context_from_the_plan(music):
    p = plan(("red", 1, 11, 0), seed=5, strobe_max_rate=40.0)
    ctx = render_context(GEO, music, 30, p)
    assert ctx.seed == 5 and ctx.palette == ("#ff0000", "#0000ff")
    assert ctx.strobe_rate == 15.0                                  # half of 30 FPS
    assert render_context(GEO, music, 120, p).strobe_rate == 40.0


# -- refusals
def test_stale_plans_are_refused(music, methods):
    p = plan(("red", 1, 11, 0))
    with pytest.raises(StalePlan, match="director version"):
        renderer(music, methods, p.model_copy(update={"director": DirectorInfo(version=DIRECTOR_VERSION + 1)}))
    seg = p.segments[0]
    for s, msg in ((seg.model_copy(update={"method_version": 2}), "version 2"),
                   (seg.model_copy(update={"method": "gone"}), "not registered")):
        with pytest.raises(StalePlan, match=msg):
            renderer(music, methods, p.model_copy(update={"segments": [s]}))
    with pytest.raises(ValidationError):
        renderer(music, methods, plan(("red", 1, 11, 0, {"size": 2.0})))


def test_bad_method_output(music, methods):
    class F64(methods["red"]):
        id = "f64"

        def render(self, start, stop):
            return np.zeros((stop - start, self.ctx.pixel_count, 3))

    class NaN(methods["red"]):
        id = "nan"

        def render(self, start, stop):
            return np.full((stop - start, self.ctx.pixel_count, 3), np.nan, np.float32)

    ms = {**methods, "f64": F64, "nan": NaN}
    with pytest.raises(TypeError, match="float64"):
        renderer(music, ms, plan(("f64", 1, 11, 0))).render(0, 10)
    with pytest.raises(ValueError, match="NaN"):
        renderer(music, ms, plan(("nan", 1, 11, 0))).render(0, 10)
    with pytest.raises(ValueError, match="outside"):
        renderer(music, ms, plan(("red", 1, 11, 0))).render(0, 10_000)


# -- the job
def test_render_anim(tmp_path, music, methods):
    p = plan(("clock", 1, 5, 0), ("red", 5, 11, 2.0))
    path = tmp_path / "x.anim"
    seen = []
    meta = render_anim(path, render_context(GEO, music, 30, p), p, "r1",
                       progress=lambda done, n: seen.append((done, n)), methods=methods)
    assert seen[-1] == (600, 600) and len(seen) == 20                # 1 s chunks
    with AnimReader(path) as rd:
        assert rd.meta == meta
        assert (rd.meta["fps"], rd.meta["frame_count"], rd.meta["chunk_frames"]) == (30, 600, 30)
        assert rd.meta["generator"] == {"id": GENERATOR_ID, "version": DIRECTOR_VERSION}
        assert rd.meta["track_sha256"] == SHA and rd.meta["geometry_hash"] == GEO.hash
        assert rd.meta["analysis"] == {"version": 2, "tempo_override": None}
        assert rd.meta["plan"]["segments"][0]["start"] == 0.0           # retimed
        assert rd.meta["params"] == p.director.params
        frames = np.concatenate([rd.chunk(k) for k in range(rd.chunk_count)])
    assert np.array_equal(frames, renderer(music, methods, p, fps=30).render(0, 600))


def test_cancel_leaves_no_file(tmp_path, music, methods):
    p = plan(("red", 1, 11, 0))
    path = tmp_path / "x.anim"
    with pytest.raises(RenderCancelled):
        render_anim(path, render_context(GEO, music, 30, p), p, "r1", cancelled=lambda: True, methods=methods)
    assert not path.exists() and not anim_tmp_path(path).exists()
