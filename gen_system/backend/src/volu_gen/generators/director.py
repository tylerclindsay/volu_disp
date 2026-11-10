"""The director: a track's music and track params → a plan
(docs/generators.md → Director).

1. **Units.** The track is cut at phrase starts (variable tempo: section
   starts and drops), from bar 1 to the end.
2. **Levels.** Each unit is `high` (a drop), `low` (a breakdown, or quiet)
   or `medium`, from its phrase tags and mean per-bar mix energy. A unit
   shorter than MIN_UNIT_BARS (a section's short tail) then joins the one
   before it, unless it starts a drop.
3. **Segments.** Consecutive units of one level in one section merge up to
   the level's segment length (so calm parts hold a method for a whole
   section), and each run is split into segments of that length. Pinned
   segments are cut out of the result unchanged.
4. **Methods.** Per segment, each enabled method scores by its `suits` ×
   its track-param weight; one is drawn by weighted random choice, never
   the previous segment's method if another fits. It then picks its own
   params.
5. **Transitions.** A drop always cuts; otherwise the contrast between
   neighboring segments picks a cut or a crossfade length.

Pure Python over `Music` without audio, so it runs in the API server
(decision 0011). Deterministic: the same music, track params, pinned
segments, seed and versions give the same plan.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Annotated

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .context import STROBE_MAX_RATE
from .method import METHODS, PLAN_STREAM, TRACK_STREAM, Method, SegmentInfo, Suitability, segment_rng
from .music import Music
from .palette import PALETTES, HexColor, choose_palette, mood_scores
from .plan import DirectorInfo, Plan, Segment, Transition

DIRECTOR_VERSION = 1  # the generator version: bump when plans change for the same inputs

# Levels: `high` is a drop phrase; `low` a breakdown or under LOW_DB (per-bar
# mix energy, dB re. the track's loud level); the rest `medium`.
LOW_DB = -10.0    # (tune)
SEGMENT_BARS = {"low": 16, "medium": 4, "high": 2}   # at change rate 1 (tune)
MAX_SEGMENT_BARS = 64
MIN_UNIT_BARS = 2

# A method's energy fit falls off as exp(−(d / ENERGY_SOFT_DB)²), d the dB
# outside its `suits` range (tune).
ENERGY_SOFT_DB = 6.0

# Transition contrast (generators.md → open question 1: tune by eye).
CONTRAST_DB = 6.0          # an energy jump this big (2 bars either side) adds 1
CONTRAST_EVENT = 0.5       # per drums/bass in/out within a bar of the boundary (≤ 1 in all)
CONTRAST_SECTION = 0.5     # the boundary is a section start
CONTRAST_PHRASE = 0.25     # ... or a phrase start (a split inside a phrase adds 0)
CONTRAST_TAGS = 0.5        # the phrase tags change
CUT_AT = 1.0               # contrast at or above: a hard cut
FADES = ((0.5, 1.0), (0.25, 2.0))   # (contrast at or above, crossfade beats); under: one bar
DRUMS_BASS_EVENTS = frozenset({"drums_in", "drums_out", "bass_in", "bass_out"})


class DirectorParams(BaseModel):
    """Track params (Studio): constant for the whole track.

    intensity        overall brightness, 0–1; applied by the render job
    change_rate      how quickly segments switch, × the default
    palette          "auto" (from the track's mood), a palette name, or colors
    weights          per method id: how often the director picks it (default 1)
    disabled         method ids it never picks
    strobe_max_rate  flashes/s; the render also caps it at half the FPS
    seed             re-roll for a different plan
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    intensity: float = Field(1.0, ge=0.0, le=1.0)
    change_rate: float = Field(1.0, ge=0.25, le=4.0)
    palette: str | list[HexColor] = "auto"
    weights: dict[str, Annotated[float, Field(ge=0.0)]] = {}
    disabled: list[str] = []
    strobe_max_rate: float = Field(STROBE_MAX_RATE, gt=0.0, le=STROBE_MAX_RATE)
    seed: int = Field(0, ge=0)

    @field_validator("palette")
    @classmethod
    def _palette(cls, v):
        if isinstance(v, str) and v != "auto" and v not in PALETTES:
            raise ValueError(f"unknown palette {v!r}; 'auto', one of {sorted(PALETTES)}, or colors")
        if isinstance(v, list) and not v:
            raise ValueError("a custom palette needs at least one color")
        return v

    def weight(self, method_id: str) -> float:
        return 0.0 if method_id in self.disabled else self.weights.get(method_id, 1.0)


@dataclass(frozen=True)
class _Unit:
    start_bar: int
    end_bar: int
    level: str
    section: int
    drop: bool   # starts at a drop

    @property
    def bars(self) -> int:
        return self.end_bar - self.start_bar


@dataclass(frozen=True)
class _Structure:
    """Bar numbers where things start, for segmenting and transitions."""
    end_bar: int            # one past the last bar
    sections: list[int]     # sorted section start bars
    phrases: frozenset[int]
    drops: frozenset[int]

    @classmethod
    def of(cls, music: Music) -> _Structure:
        return cls(music.bar_count + 1,
                   sorted({s["start_bar"] for s in music.sections}),
                   frozenset(p["start_bar"] for p in music.phrases),
                   frozenset(p["start_bar"] for p in music.phrases if "drop" in (p["tags"] or [])))

    def section_of(self, bar: int) -> int:
        return max(bisect_right(self.sections, bar) - 1, 0)


def make_plan(music: Music, params: DirectorParams = DirectorParams(),
              pinned: Sequence[Segment] = (), mood_z: dict[str, float] | None = None,
              methods: Mapping[str, type[Method]] = METHODS) -> Plan:
    """Plan a track.

    pinned  segments to keep (a previous plan's pinned segments): their
            bars, method and overrides stay; params and transition are
            re-chosen, and the rest of the track is planned around them
    mood_z  library-wide mood z-scores for the palette choice (decision
            0004); default: the track's cosines standardized on their own
    methods the methods to choose from (default: the registry)
    """
    st = _Structure.of(music)
    spans = _carve(_segment_bars(_units(music, st), params.change_rate), pinned, methods, st.end_bar)
    candidates = [(i, methods[i], params.weight(i)) for i in sorted(methods) if params.weight(i) > 0]
    if not candidates:
        raise ValueError("no methods enabled")

    segments: list[Segment] = []
    prev: SegmentInfo | None = None
    for a, b, pin in spans:
        info = SegmentInfo.from_bars(music, a, b)
        rng = segment_rng(params.seed, a, PLAN_STREAM)
        draw = rng.random()   # drawn for pinned segments too, so their params match an unpinned plan's
        if pin is None:
            cls = _choose(info, candidates, segments[-1].method if segments else None, draw)
        else:
            cls = methods[pin.method]
        chosen = cls.choose_params(info, music, rng).model_dump(mode="json")
        overrides = {}
        if pin is not None:
            overrides = {k: v for k, v in pin.overrides.items() if k in cls.Params.model_fields}
            cls.Params.model_validate({**chosen, **overrides})   # a bad edit fails here, not at render
        segments.append(Segment(
            start_bar=a, end_bar=b, method=cls.id, method_version=cls.version, params=chosen,
            overrides=overrides, pinned=pin is not None,
            transition=Transition() if prev is None else _transition(music, st, prev, info)))
        prev = info

    return Plan(director=DirectorInfo(version=DIRECTOR_VERSION, params=params.model_dump(mode="json")),
                palette=_palette(music, params, mood_z), segments=segments).retime(music)


def replan(music: Music, params: DirectorParams, plan: Plan, **kw) -> Plan:
    """Plan again, keeping `plan`'s pinned segments."""
    return make_plan(music, params, [s for s in plan.segments if s.pinned], **kw)


# -- segmenting
def _merge_short(units: list[_Unit]) -> list[_Unit]:
    """A unit shorter than MIN_UNIT_BARS joins the one before (a short first
    unit joins the next) and takes its level, unless it starts a drop or
    would move one."""
    out: list[_Unit] = []
    for u in units:
        if out and u.bars < MIN_UNIT_BARS and not u.drop:
            out[-1] = replace(out[-1], end_bar=u.end_bar)
        elif len(out) == 1 and out[0].bars < MIN_UNIT_BARS and not u.drop:
            out[0] = replace(u, start_bar=out[0].start_bar)
        else:
            out.append(u)
    return out


def _level(info: SegmentInfo) -> str:
    """`high` is drops only: most of an electronic track is loud (median bar
    ≈ −4 dB), and switching every 2 bars through all of it is frantic."""
    if "drop" in info.tags:
        return "high"
    if ("breakdown" in info.tags and "buildup" not in info.tags) or info.energy_db < LOW_DB:
        return "low"
    return "medium"


def _units(music: Music, st: _Structure) -> list[_Unit]:
    if music.mode == "grid":
        starts = set(st.phrases)
    else:   # variable tempo: bars are raw downbeats, so 8-bar phrases mean little
        starts = set(st.sections) | st.drops
    bounds = sorted({1} | {b for b in starts if 1 <= b < st.end_bar}) + [st.end_bar]
    units = [_Unit(a, b, _level(SegmentInfo.from_bars(music, a, b)), st.section_of(a), a in st.drops)
             for a, b in zip(bounds, bounds[1:])]
    return _merge_short(units)


def _length(level: str, change_rate: float) -> int:
    n = 2 ** round(math.log2(SEGMENT_BARS[level] / change_rate))
    return int(min(max(n, 1), MAX_SEGMENT_BARS))


def _segment_bars(units: list[_Unit], change_rate: float) -> list[tuple[int, int]]:
    """(start_bar, end_bar) of each automatic segment."""
    runs: list[list[_Unit]] = []
    for u in units:
        r = runs[-1] if runs else None
        if (r and not u.drop and u.level == r[0].level and u.section == r[0].section
                and u.end_bar - r[0].start_bar <= _length(u.level, change_rate)):
            r.append(u)
        else:
            runs.append([u])
    spans = []
    for r in runs:
        n = _length(r[0].level, change_rate)
        a, end = r[0].start_bar, r[-1].end_bar
        cuts = list(range(a, end, n)) + [end]
        if len(cuts) > 2 and end - cuts[-2] <= n / 2:   # a short remainder joins the last segment
            del cuts[-2]
        spans += list(zip(cuts, cuts[1:]))
    return spans


def _carve(spans: list[tuple[int, int]], pinned: Sequence[Segment], methods: Mapping[str, type[Method]],
           end_bar: int) -> list[tuple[int, int, Segment | None]]:
    """The automatic spans with the pinned segments cut in. Pins are clipped
    to the track (a grid override can shorten it); pins outside it, or of a
    method no longer registered, are dropped."""
    lo = spans[0][0]
    pins = []
    for p in sorted(pinned, key=lambda s: s.start_bar):
        a, b = max(p.start_bar, lo), min(p.end_bar, end_bar)
        if b > a and p.method in methods:
            if pins and a < pins[-1][1]:
                raise ValueError(f"pinned segments overlap at bar {a}")
            pins.append((a, b, p))
    out: list[tuple[int, int, Segment | None]] = list(pins)
    for a, b in spans:
        for pa, pb, _ in pins:
            if pb <= a or pa >= b:
                continue
            if pa > a:
                out.append((a, pa, None))
            a = pb
        if a < b:
            out.append((a, b, None))
    return sorted(out, key=lambda s: s[0])


# -- choosing a method
def _fit(suits: Suitability, info: SegmentInfo) -> float:
    if info.bars < suits.min_bars or not (info.tags & suits.phrase_types):
        return 0.0
    lo, hi = suits.energy_db
    d = max(lo - info.energy_db, info.energy_db - hi, 0.0)
    return math.exp(-(d / ENERGY_SOFT_DB) ** 2)


def _choose(info: SegmentInfo, candidates: list[tuple[str, type[Method], float]], prev: str | None,
            draw: float) -> type[Method]:
    """Weighted random choice by fit × weight with one uniform draw; never
    the previous method if another scores. If nothing fits, by weight alone."""
    w = np.array([wt * _fit(cls.suits, info) for _, cls, wt in candidates])
    if not w.any():
        w = np.array([wt for _, _, wt in candidates])
    ids = [i for i, _, _ in candidates]
    if prev in ids and (np.delete(w, ids.index(prev)) > 0).any():
        w[ids.index(prev)] = 0.0
    c = np.cumsum(w / w.sum())
    return candidates[min(int(np.searchsorted(c, draw, side="right")), len(c) - 1)][1]


# -- transitions
def _contrast(music: Music, st: _Structure, prev: SegmentInfo, seg: SegmentInfo) -> float:
    """How different two neighboring segments are: 0 for a split inside one
    phrase of the same material, 1 and up for a hard change."""
    b = seg.start_bar
    c = 0.0
    if "mix" in music.energy_lanes:
        before, after = music.bar_energy_db("mix", b - 2, b), music.bar_energy_db("mix", b, b + 2)
        if len(before) and len(after):
            c += min(abs(float(after.mean() - before.mean())) / CONTRAST_DB, 1.0)
    events = sum(1 for e in music.events if e["event"] in DRUMS_BASS_EVENTS and abs(e["bar"] - b) <= 1)
    c += min(events * CONTRAST_EVENT, 1.0)
    c += CONTRAST_SECTION if b in st.sections else CONTRAST_PHRASE if b in st.phrases else 0.0
    if prev.tags != seg.tags:
        c += CONTRAST_TAGS
    return c


def _transition(music: Music, st: _Structure, prev: SegmentInfo, seg: SegmentInfo) -> Transition:
    if seg.start_bar in st.drops:
        return Transition()
    c = _contrast(music, st, prev, seg)
    if c >= CUT_AT:
        return Transition()
    beats = next((n for at, n in FADES if c >= at), float(music.meter))
    cap = max(min(prev.bars, seg.bars) * music.meter // 4, 1)   # at most a quarter of either segment
    return Transition(kind="crossfade", beats=float(min(beats, cap)))


# -- palette
def _palette(music: Music, params: DirectorParams, mood_z: dict[str, float] | None) -> list[str]:
    if isinstance(params.palette, list):
        return list(params.palette)
    name = params.palette
    if name == "auto":
        z = mood_z if mood_z is not None else mood_scores(music.mood)
        name = choose_palette(z, segment_rng(params.seed, 0, TRACK_STREAM))
    return list(PALETTES[name][0])
