import numpy as np
import pytest
from pydantic import ValidationError

from volu_gen.analysis.grid import grid_times
from volu_gen.generators import (DIRECTOR_VERSION, PALETTES, DirectorParams, Method, MethodParams, Music,
                                 Plan, RenderContext, Suitability, hex_to_linear, make_plan, replan)
from volu_gen.generators.director import _merge_short, _Unit
from volu_gen.geometry import Geometry, GeometrySpec

BPM, OFFSET, DUR = 120.0, 0.5, 80.0   # bars of 2 s; bar 1 at 0.5 s; bars 1–40

# section start bar → its phrases (start bar, bars, tags, mix dB per bar)
TRACK = {
    1: [(1, 8, [], -14.0), (9, 8, ["breakdown"], -12.0)],    # calm intro
    17: [(17, 8, ["buildup"], -6.0)],
    25: [(25, 8, ["drop"], 0.0)],
    33: [(33, 7, [], -5.0), (40, 1, ["breakdown"], -20.0)],  # outro with a 1-bar tail
}


def analysis(mode: str = "grid") -> dict:
    beats, downbeats = grid_times(BPM, OFFSET, DUR)
    bar = lambda b: float(downbeats[b - 1])
    sections, phrases, energy = [], [], []
    for i, (s, ps) in enumerate(TRACK.items()):
        sections.append({"start": bar(s), "start_bar": s})
        for start, n, tags, db in ps:
            phrases.append({"start": bar(start), "start_bar": start, "bars": n, "section": i, "tags": tags})
            energy += [db] * n
    return {
        "version": 2, "source": {"duration": DUR},
        "tempo": {"mode": mode, "bpm": BPM, "offset": OFFSET, "meter": 4,
                  "beats": beats.tolist(), "downbeats": downbeats.tolist()},
        "sections": sections, "phrases": phrases,
        "events": [{"bar": 25, "time": bar(25), "event": "drums_in"}],
        "bars": {"energy_db": {"mix": energy}},
        "mood": {"cos": {"calm": 0.2, "dark": 0.3, "energetic": 0.25}},
    }


def method(method_id: str, **suits) -> type[Method]:
    class M(Method):
        id = method_id
        version = 1

        class Params(MethodParams):
            x: float = 0.0
            size: float = 1.0

        @classmethod
        def choose_params(cls, seg, music, rng):
            return cls.Params(x=float(rng.random()))

        def render(self, start, stop): ...

    M.suits = Suitability(**suits)
    return M


CALM = method("calm", phrase_types=frozenset({"breakdown", "groove", "buildup"}), energy_db=(-60, -8),
              min_bars=4)
SHAPES = method("shapes", energy_db=(-15, 6), min_bars=2)
FLASH = method("flash", phrase_types=frozenset({"drop"}), energy_db=(-6, 6))
METHODS = {m.id: m for m in (CALM, SHAPES, FLASH)}


@pytest.fixture
def music() -> Music:
    return Music(analysis())


def plan(music, **kw) -> Plan:
    pinned = kw.pop("pinned", ())
    return make_plan(music, DirectorParams(**kw), pinned, methods=METHODS)


def bars(p: Plan) -> list[tuple[int, int]]:
    return [(s.start_bar, s.end_bar) for s in p.segments]


# -- segmenting
def test_segments(music):
    assert music.bar_count == 40
    p = plan(music)
    assert bars(p) == [(1, 17),                                  # low: intro + breakdown, one section
                       (17, 21), (21, 25),                       # medium: every 4 bars
                       (25, 27), (27, 29), (29, 31), (31, 33),   # drop: every 2 bars
                       (33, 37), (37, 41)]                       # medium; the 1-bar tail joins it
    assert (p.segments[0].start, p.segments[-1].end) == (0.0, DUR)   # retimed, covers the track
    assert p.segments[3].start == pytest.approx(48.5)
    assert p.director.version == DIRECTOR_VERSION and p.director.params == DirectorParams().model_dump()
    assert Plan.model_validate_json(p.model_dump_json()) == p


@pytest.mark.parametrize("rate, expect", [
    (2.0, [(1, 9), (9, 17), (17, 19), (19, 21), (21, 23), (23, 25)] + [(b, b + 1) for b in range(25, 33)]
     + [(33, 35), (35, 37), (37, 39), (39, 41)]),
    (0.5, [(1, 17), (17, 25), (25, 29), (29, 33), (33, 41)]),
])
def test_change_rate(music, rate, expect):
    assert bars(plan(music, change_rate=rate)) == expect


def test_variable_tempo_segments_by_sections():
    p = plan(Music(analysis("variable")))
    starts = [s.start_bar for s in p.segments]
    assert {1, 17, 25, 33} <= set(starts)
    assert starts[:2] == [1, 17]                                 # no cut at the phrase start, bar 9


def test_merge_short():
    u = lambda a, b, level="medium", drop=False: _Unit(a, b, level, 0, drop)
    # a short first unit joins the next, a short tail the one before; both take its level
    assert _merge_short([u(1, 2, "low"), u(2, 9), u(9, 10, "low"), u(10, 11, drop=True)]) == \
        [u(1, 10), u(10, 11, drop=True)]                         # a short drop stays
    assert _merge_short([u(1, 2), u(2, 9, "high", drop=True)]) == [u(1, 2), u(2, 9, "high", drop=True)]


# -- methods
def test_methods_fit_segments(music):
    for seed in range(20):
        p = plan(music, seed=seed)
        for s in p.segments:
            info = s.info(music)
            if "drop" in info.tags:
                assert s.method in ("flash", "shapes")
            else:
                assert s.method != "flash"
            if info.bars < 4:
                assert s.method != "calm"
        assert all(a.method != b.method for a, b in zip(p.segments, p.segments[1:]))
        assert all(0 <= s.params["x"] < 1 for s in p.segments)


def test_deterministic_and_seeded(music):
    assert plan(music, seed=3) == plan(music, seed=3)
    assert plan(music, seed=3).segments != plan(music, seed=4).segments


def test_weights_and_disabled(music):
    p = plan(music, disabled=["shapes"])
    assert {s.method for s in p.segments[3:7]} == {"flash"}       # repeats when nothing else fits
    q = plan(music, weights={"shapes": 0.0})
    assert [s.method for s in q.segments] == [s.method for s in p.segments]
    with pytest.raises(ValueError, match="no methods"):
        plan(music, disabled=["calm", "shapes", "flash"])
    with pytest.raises(ValidationError):
        DirectorParams(weights={"calm": -1.0})


# -- transitions
def test_transitions(music):
    t = [(s.transition.kind, s.transition.beats) for s in plan(music).segments]
    assert t[0] == ("cut", 0)
    assert t[1] == ("cut", 0)               # section start, tags change, +6 dB
    assert t[2] == ("crossfade", 4)         # split inside a phrase, same material: one bar
    assert t[3] == ("cut", 0)               # into a drop: always a cut
    assert t[4] == ("crossfade", 2)         # one bar, capped at a quarter of a 2-bar segment
    assert t[7] == ("cut", 0)               # section start, tags change, −5 dB
    assert t[8] == ("crossfade", 1)         # inside a phrase, but the breakdown tail changes the tags


# -- pinned segments
def test_pinned_survive_replan(music):
    p = plan(music, seed=1)
    seg = p.segments[2].model_copy(update={"overrides": {"size": 3.0}, "pinned": True})
    p.segments[2] = seg
    q = replan(music, DirectorParams(seed=2, change_rate=2.0), p, methods=METHODS)
    kept = [s for s in q.segments if s.pinned]
    assert len(kept) == 1
    k = kept[0]
    assert (k.start_bar, k.end_bar, k.method, k.overrides) == (21, 25, seg.method, {"size": 3.0})
    assert (19, 21) in bars(q) and (25, 26) in bars(q)           # the rest re-planned around it
    same = replan(music, DirectorParams(seed=1), p, methods=METHODS)
    assert same.segments[2].params == p.segments[2].params      # pinning doesn't change auto fields
    assert [s.method for s in same.segments] == [s.method for s in p.segments]


def test_pinned_cut_across_segments(music):
    pin = plan(music).segments[0].model_copy(update={"start_bar": 26, "end_bar": 30, "pinned": True,
                                                     "method": "calm"})
    p = plan(music, pinned=[pin])
    assert bars(p)[3:7] == [(25, 26), (26, 30), (30, 31), (31, 33)]
    assert p.segments[4].method == "calm"                        # kept, though it doesn't suit a drop


def test_pinned_edge_cases(music):
    base = plan(music).segments[0]
    pin = lambda a, b, **kw: base.model_copy(update={"start_bar": a, "end_bar": b, "pinned": True, **kw})
    assert bars(plan(music, pinned=[pin(37, 50)]))[-1] == (37, 41)        # clipped to the track
    assert len(plan(music, pinned=[pin(45, 50)]).segments) == 9           # outside: dropped
    assert not any(s.pinned for s in plan(music, pinned=[pin(1, 5, method="gone")]).segments)
    p = plan(music, pinned=[pin(1, 5, overrides={"size": 2.0, "removed": 1})])
    assert p.segments[0].overrides == {"size": 2.0}                       # unknown fields dropped
    with pytest.raises(ValidationError):
        plan(music, pinned=[pin(1, 5, overrides={"size": "big"})])
    with pytest.raises(ValueError, match="overlap"):
        plan(music, pinned=[pin(1, 5), pin(4, 8)])


# -- palette
def test_palette(music):
    auto = plan(music).palette
    assert any(auto == list(c) for c, _ in PALETTES.values())
    assert plan(music, palette="ice").palette == list(PALETTES["ice"][0])
    assert plan(music, palette=["#102030"]).palette == ["#102030"]
    for bad in ("nope", [], ["red"]):
        with pytest.raises(ValidationError):
            DirectorParams(palette=bad)


def test_palette_follows_mood(music):
    calm = {"calm": 3.0, "atmospheric": 3.0, "dreamy": 2.0, "melancholic": 2.0}
    picks = [make_plan(music, DirectorParams(seed=s), mood_z=calm, methods=METHODS).palette
             for s in range(40)]
    calm_palettes = [list(PALETTES[n][0]) for n in ("ocean", "ice")]
    assert sum(p in calm_palettes for p in picks) > 30


def test_context_colors(music):
    geo = Geometry.from_spec(GeometrySpec((2, 2, 2), 50.0, 50.0, 100.0, (25.0, 25.0)))
    ctx = RenderContext(geo, music, fps=120, palette=("#ffffff", "#000000", "#808080"))
    assert ctx.colors == pytest.approx(np.array([[1, 1, 1], [0, 0, 0], [0.2159] * 3]), abs=1e-4)
    assert len(RenderContext(geo, music, fps=120).colors)                 # default palette
    assert hex_to_linear([]).shape == (0, 3)
