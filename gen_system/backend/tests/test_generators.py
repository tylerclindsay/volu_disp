import sys

import numpy as np
import pytest
from pydantic import ValidationError

from volu_gen.analysis.audio import BASE_SR
from volu_gen.analysis.features import N_BANDS
from volu_gen.analysis.grid import grid_times
from volu_gen.geometry import Geometry, GeometrySpec
from volu_gen.generators import (GROOVE, METHODS, Method, MethodParams, Music, Plan, RenderContext,
                                 SegmentInfo, Suitability, get_method, normalized_positions, register,
                                 segment_rng)

BPM, OFFSET, DUR = 120.0, 0.5, 20.0   # bars of 2 s; bar 1 at 0.5 s


def analysis() -> dict:
    beats, downbeats = grid_times(BPM, OFFSET, DUR)
    return {
        "version": 2, "source": {"duration": DUR},
        "tempo": {"mode": "grid", "bpm": BPM, "offset": OFFSET, "meter": 4,
                  "beats": beats.tolist(), "downbeats": downbeats.tolist()},
        "sections": [{"start": 0.0, "end": 8.5, "start_bar": 1}, {"start": 8.5, "end": DUR, "start_bar": 5}],
        "phrases": [{"start": 0.5, "end": 8.5, "start_bar": 1, "bars": 4, "section": 0, "tags": []},
                    {"start": 8.5, "end": 16.5, "start_bar": 5, "bars": 4, "section": 1, "tags": ["drop"]}],
        "events": [],
        "bars": {"energy_db": {"mix": [-20.0, -20, -20, -20, 0, 0, 0, 0, -10, -10]}},
        "genre": {"top": [{"label": "Electronic---Techno", "score": 0.5}]},
        "mood": {"cos": {"dark": 0.3}},
    }


def features() -> dict:
    sr, env_hop, spec_hop = BASE_SR, 147, 441
    n = int(DUR * sr)
    n_env, n_spec = n // env_hop + 1, n // spec_hop + 1
    env = np.zeros((5, n_env), np.float16)
    env[0] = np.linspace(0, 1, n_env)                     # mix ramps 0 → 1 (× env_ref)
    spec = np.zeros((5, N_BANDS, n_spec), np.float16)
    spec[0] = np.arange(n_spec)[None, :] % 2 * -10        # alternates 0, −10 dB per frame
    width = np.full((5, n_spec), 0.5, np.float16)
    kick = (np.array([1.0, 2.0, 3.5]), np.array([1.0, 0.5, 0.8], np.float32))
    return {"version": 3, "sample_rate": sr, "env_hop": env_hop, "spec_hop": spec_hop, "n_fft": 4096,
            "n_samples": n, "lanes": ["mix", "vocals", "drums", "bass", "other"], "classes": ["kick", "hat"],
            "band_edges": np.geomspace(40, 16000, N_BANDS + 1), "env": env,
            "env_ref": np.full(5, 0.5, np.float32), "spec": spec, "width": width,
            "onsets": {"kick": kick, "hat": (np.zeros(0), np.zeros(0, np.float32))},
            "env_t": np.arange(n_env) * env_hop / sr, "spec_t": np.arange(n_spec) * spec_hop / sr}


@pytest.fixture
def music() -> Music:
    ramp = np.tile(np.arange(int(DUR * BASE_SR), dtype=np.float32) / BASE_SR, (2, 1))
    return Music(analysis(), features(), lambda lane: ramp)   # sample value = its time, s


@pytest.fixture
def ctx(music) -> RenderContext:
    geo = Geometry.from_spec(GeometrySpec((4, 6, 3), 50.0, 50.0, 100.0, (25.0, 25.0)))
    return RenderContext(geo, music, fps=97, seed=7)


# -- Music: grid
def test_bars_and_beats(music):
    assert music.bar_time([1, 2, 1.5]) == pytest.approx([0.5, 2.5, 1.5])
    assert music.bar_time(0) == pytest.approx(-1.5)               # extrapolated before bar 1
    assert music.bar_at([0.5, 3.5, -1.5]) == pytest.approx([1.0, 2.5, 0.0])
    t = np.array([0.5, 0.75, 1.0])
    assert music.beat_phase(t) == pytest.approx([0.0, 0.5, 0.0])
    assert music.bar_phase(1.5) == pytest.approx(0.5)
    assert music.beat_length(5.0) == pytest.approx(0.5)


def test_variable_tempo_reads_like_a_grid():
    a = analysis()
    a["tempo"].update(mode="variable", beats=[0.0, 0.5, 1.5, 2.0], downbeats=[0.0, 1.5, 4.5])
    m = Music(a)
    assert m.bar_at([0.75, 3.0, 6.0]) == pytest.approx([1.5, 2.5, 3.5])
    assert m.beat_at(1.0) == pytest.approx(1.5)


def test_structure(music):
    assert list(music.phrase_index([0.0, 0.5, 9.0, 19.0])) == [-1, 0, 1, 1]
    assert list(music.section_index([0.0, 9.0])) == [0, 1]
    assert music.energy_db("mix", [0.5, 1.5, 8.5, 9.5]) == pytest.approx([-20, -20, -10, 0])
    assert list(music.bar_energy_db("mix", 4, 6)) == [-20, 0]
    assert music.genre[0]["label"] == "Electronic---Techno"
    assert music.mood == {"dark": 0.3}


# -- Music: features
def test_env_and_spectrum_interpolate(music):
    f = music.features
    t = f["env_t"][[0, -1]]
    assert music.env("mix", t) == pytest.approx([0.0, 2.0], abs=1e-3)   # 1.0 → 2× env_ref
    assert music.env("mix", f["env_t"][-1], db=True) == pytest.approx(6.02, abs=0.01)
    assert music.env("bass", 1.0, db=True) == -120.0
    half = (f["spec_t"][0] + f["spec_t"][1]) / 2
    s = music.spectrum("mix", np.array([f["spec_t"][1], half]))
    assert s.shape == (2, N_BANDS)
    assert s[:, 0] == pytest.approx([-10, -5])
    assert music.width("drums", 3.0) == pytest.approx(0.5)


def test_onsets_since_until(music):
    t, s = music.onsets("kick", 1.5, 4.0)
    assert list(t) == [2.0, 3.5]
    assert list(music.onsets("kick", min_strength=0.7)[0]) == [1.0, 3.5]
    dt, st = music.since("kick", np.array([0.5, 1.0, 2.25, 9.0]))
    assert dt == pytest.approx([np.inf, 0.0, 0.25, 5.5])
    assert list(st) == pytest.approx([0.0, 1.0, 0.5, 0.8])
    dt, st = music.until("kick", np.array([0.5, 1.0, 9.0]))
    assert dt == pytest.approx([0.5, 1.0, np.inf])
    assert music.since("hat", np.array([1.0]))[0][0] == np.inf


def test_samples_and_windows(music):
    s = music.samples("drums", 1.0, 1.001)
    assert s.shape == (44, 2) and s[0, 0] == pytest.approx(1.0)
    assert not music.samples("mix", -1.0, -0.5).any()                  # outside the track
    w = music.windows("bass", np.array([0.0, 2.0]), 4)
    assert w.shape == (2, 4, 2)
    assert not w[0].any()                                              # before the start
    assert w[1, -1, 0] == pytest.approx(2.0 - 1 / BASE_SR)             # last sample before t
    with pytest.raises(ValueError):
        music.samples("kick", 0, 1)
    with pytest.raises(RuntimeError):
        Music(analysis(), features()).samples("mix", 0, 1)            # planning: no audio


def test_no_features():
    m = Music(analysis())
    assert m.bar_at(0.5) == 1.0
    with pytest.raises(RuntimeError, match="features"):
        m.env("mix", 1.0)


# -- RenderContext
def test_normalized_positions():
    p = normalized_positions(np.array([[0, 0, 0], [100, 200, 50]], np.float64))
    assert p.tolist() == [[-0.5, 1.0, -0.25], [0.5, -1.0, 0.25]]       # y up, aspect kept


def test_context(ctx):
    assert ctx.pixel_count == 72
    assert np.abs(ctx.pos).max() == pytest.approx(1.0)
    assert ctx.pos[0, 1] > ctx.pos[1, 1]                               # pixel 0 is the top
    assert list(np.unique(ctx.parity)) == [0, 1]
    assert ctx.frame_count == 1940                                     # ceil(20 × 97)
    assert ctx.times(97, 99) == pytest.approx([1.0, 1 + 1 / 97])
    assert ctx.frame_at(1.0) == 97
    with pytest.raises(ValueError):
        RenderContext(ctx.geometry, ctx.music, fps=20)


# -- Methods
class Pulse(Method):
    """Test method: every pixel at the kick decay, in a random color."""
    id = "test_pulse"
    version = 1

    class Params(MethodParams):
        decay: float = 0.2

    suits = Suitability(phrase_types=frozenset({"drop"}))

    @classmethod
    def choose_params(cls, seg, music, rng):
        return cls.Params(decay=0.1 if "drop" in seg.tags else 0.3)

    def __init__(self, ctx, seg, params):
        super().__init__(ctx, seg, params)
        self.color = self.rng.random(3).astype(np.float32)

    def render(self, start, stop):
        dt, s = self.ctx.music.since("kick", self.ctx.times(start, stop))
        v = (s * np.exp(-dt / self.params.decay)).astype(np.float32)
        return np.broadcast_to(v[:, None, None] * self.color, (stop - start, self.ctx.pixel_count, 3))


register(Pulse)


def test_registry():
    assert get_method("test_pulse") is Pulse and METHODS["test_pulse"] is Pulse
    with pytest.raises(KeyError):
        get_method("nope")

    class Bad(Method):
        id = "test_pulse"
        version = 1
        Params = Pulse.Params

        def render(self, start, stop): ...

    with pytest.raises(ValueError, match="already registered"):
        register(Bad)
    Bad.id, Bad.version = "test_bad", 0
    with pytest.raises(TypeError, match="version"):
        register(Bad)
    with pytest.raises(ValueError):
        Suitability(phrase_types=frozenset({"intro"}))


def test_segment_info(music):
    a = SegmentInfo.from_bars(music, 3, 7)
    assert (a.start, a.end, a.bars) == (4.5, 12.5, 4)
    assert a.tags == {GROOVE, "drop"}
    assert a.energy_db == pytest.approx(-10.0)
    b = SegmentInfo.from_bars(music, 0, 2)                             # lead-in clipped to the track
    assert (b.start, b.end, b.tags) == (0.0, 2.5, {GROOVE})


def test_segment_rng():
    a = segment_rng(7, 5, 1).random(4)
    assert np.array_equal(a, segment_rng(7, 5, 1).random(4))
    assert not np.array_equal(a, segment_rng(7, 9, 1).random(4))       # other segment
    assert not np.array_equal(a, segment_rng(7, 5, 0).random(4))       # other stream


def test_render_is_range_independent(ctx, music):
    seg = SegmentInfo.from_bars(music, 1, 3)
    m = Pulse(ctx, seg, Pulse.choose_params(seg, music, segment_rng(7, 1, 0)))
    whole = m.render(0, 400)
    parts = np.concatenate([m.render(0, 123), m.render(123, 400)])
    assert whole.dtype == np.float32 and whole.shape == (400, 72, 3)
    assert np.array_equal(whole, parts)
    again = Pulse(ctx, seg, m.params)                                  # same seed, same segment
    assert np.array_equal(again.color, m.color)


# -- Plan
def plan_json() -> dict:
    return {
        "director": {"version": 1, "params": {"seed": 7}},
        "segments": [
            {"start_bar": 0, "end_bar": 5, "method": "test_pulse", "method_version": 1,
             "params": {"decay": 0.3}},
            {"start_bar": 5, "end_bar": 11, "method": "test_pulse", "method_version": 1,
             "params": {"decay": 0.1}, "overrides": {"decay": 0.5}, "pinned": True,
             "transition": {"kind": "crossfade", "beats": 2}},
        ],
    }


def test_plan_roundtrip_and_params():
    p = Plan.model_validate(plan_json())
    assert Plan.model_validate_json(p.model_dump_json()) == p
    assert p.segments[0].method_params() == Pulse.Params(decay=0.3)
    assert p.segments[1].method_params().decay == 0.5                  # override wins
    assert p.stale_methods() == []


def test_plan_retime(music):
    p = Plan.model_validate(plan_json()).retime(music)
    assert [(s.start, s.end) for s in p.segments] == [(0.0, 8.5), (8.5, DUR)]
    assert p.segment_index(8.4) == 0 and p.segment_index(8.5) == 1
    a = analysis()                                                     # grid override: bars stay
    a["tempo"]["downbeats"] = grid_times(60.0, 0.0, DUR)[1].tolist()
    q = Plan.model_validate(plan_json()).retime(Music(a))
    assert [s.start for s in q.segments] == [0.0, 16.0]


@pytest.mark.parametrize("change", [
    lambda d: d["segments"][1].update(start_bar=6),                    # gap
    lambda d: d["segments"][0].update(end_bar=0),                      # empty
    lambda d: d["segments"][1].update(transition={"kind": "crossfade"}),
    lambda d: d["segments"][0].update(color="red"),                    # unknown field
    lambda d: d.update(segments=[]),
])
def test_plan_rejects(change):
    d = plan_json()
    change(d)
    with pytest.raises(ValidationError):
        Plan.model_validate(d)


def test_bad_override_rejected():
    d = plan_json()
    d["segments"][1]["overrides"] = {"decy": 0.5}
    with pytest.raises(ValidationError):
        Plan.model_validate(d).segments[1].method_params()


def test_no_heavy_imports():
    import subprocess
    code = "import sys, volu_gen.generators; print('torch' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
