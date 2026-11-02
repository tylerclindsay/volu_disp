import numpy as np
import pytest

from volu_gen.analysis.mood import library_z
from volu_gen.analysis.phrases import (DROP_JUMP_DB, PHRASE_BARS, bar_energy, compute_phrases,
                                      find_drops, mix_jumps, stable_states)

BAR_S = 2.0


def energies(n_bars: int, drums_off=(), bass_off=(), loud=()):
    """Per-bar dB: everything at -6 dB, stems off at -40, `loud` bars at 0.
    The low lane (drums + bass) is off only where both are."""
    e = {k: np.full(n_bars, -6.0) for k in ("mix", "drums", "bass", "other", "vocals", "low")}
    for i in drums_off:
        e["drums"][i] = -40.0
        e["mix"][i] = -12.0
        if i in bass_off:
            e["low"][i] = -40.0
    for i in bass_off:
        e["bass"][i] = -40.0
    for i in loud:
        e["mix"][i] = 0.0
    return e


def test_phrases_count_from_section_starts():
    n = 40
    downbeats = np.arange(n) * BAR_S
    # Section starts 0.4 s after bar 13's downbeat (index 12) → snapped to it.
    sections = [{"start": 0.0, "end": 24.4}, {"start": 24.4, "end": 80.0}]
    out = compute_phrases(energies(n), downbeats, n * BAR_S, sections)
    assert [s["start_bar"] for s in out["sections"]] == [1, 13]
    assert [p["start_bar"] for p in out["phrases"]] == [1, 9, 13, 21, 29, 37]
    assert [p["bars"] for p in out["phrases"]] == [8, 4, 8, 8, 8, 4]
    assert [p["section"] for p in out["phrases"]] == [0, 0, 1, 1, 1, 1]


def test_drum_changes_are_events_not_cuts():
    n = 32
    downbeats = np.arange(n) * BAR_S
    e = energies(n, drums_off=range(12, 20))
    e["mix"][:] = -6.0  # no mix jump, so the drums' return isn't a drop
    out = compute_phrases(e, downbeats, n * BAR_S, [{"start": 0.0, "end": 64.0}])
    assert [p["start_bar"] for p in out["phrases"]] == [1, 9, 17, 25]
    assert out["events"] == [{"bar": 13, "time": 24.0, "event": "drums_out"},
                             {"bar": 21, "time": 40.0, "event": "drums_in"}]
    assert len(out["bars"]["energy_db"]["drums"]) == n


def test_tags():
    n = 32
    downbeats = np.arange(n) * BAR_S
    e = energies(n, drums_off=range(8, 16), loud=range(16, 24))
    out = compute_phrases(e, downbeats, n * BAR_S, [{"start": 0.0, "end": 64.0}])
    tags = [p["tags"] for p in out["phrases"]]
    assert tags[1] == ["breakdown"]
    assert tags[2] == ["drop"]
    assert "drop" not in tags[3]


def test_stable_states_ignores_one_bar_blips():
    on = np.array([1, 1, 0, 1, 1, 0, 0, 0], bool)
    assert stable_states(on).tolist() == [1, 1, 1, 1, 1, 0, 0, 0]


def test_library_z():
    cos = {"a": {"dark": 0.30, "calm": 0.1}, "b": {"dark": 0.20, "calm": 0.1},
           "c": {"dark": 0.10, "calm": 0.4}}
    z = library_z(cos)
    assert list(z["a"]) == ["dark", "calm"]
    assert z["a"]["dark"] > 0 > z["c"]["dark"]
    assert library_z({"a": cos["a"]}) == {}


def drop_bars(out):
    return [p["start_bar"] for p in out["phrases"] if "drop" in p["tags"]]


def test_drop_off_a_phrase_start_starts_a_phrase():
    n = 40
    downbeats = np.arange(n) * BAR_S
    # Breakdown bars 7-12 (0-based), drop at bar 13 (1-based 14), off the 8-bar grid.
    e = energies(n, drums_off=range(7, 13), loud=range(13, 21))
    out = compute_phrases(e, downbeats, n * BAR_S, [{"start": 0.0, "end": 80.0}])
    assert drop_bars(out) == [14]
    assert [p["start_bar"] for p in out["phrases"]] == [1, 9, 14, 22, 30, 38]


def test_drop_found_when_kick_low_end_is_in_drums_stem():
    # The bass stem is still off in the drop's first bar (demucs put the kick's
    # low end in drums), but the drums + bass low lane is on.
    n = 32
    downbeats = np.arange(n) * BAR_S
    e = energies(n, drums_off=range(8, 16), bass_off=range(8, 17), loud=range(16, 24))
    assert e["bass"][16] == -40.0 and e["low"][16] == -6.0
    out = compute_phrases(e, downbeats, n * BAR_S, [{"start": 0.0, "end": 64.0}])
    assert drop_bars(out) == [17]


def test_no_drop_without_low_end():
    n = 32
    e = energies(n, drums_off=range(8, 16), loud=range(16, 24))
    e["low"][14:20] = -40.0
    on = {k: e[k] > -18 for k in ("drums", "bass", "low")}
    assert find_drops(e, on) == []


def test_fill_before_drop_is_not_the_drop():
    # One bar of fill (drums back, mix half way up) right before the drop:
    # the 2-bar jump peaks on the fill, but the drop is where the level arrives.
    n = 32
    e = energies(n, drums_off=range(8, 15), loud=range(16, 24))
    e["mix"][15] = -6.0
    on = {k: e[k] > -18 for k in ("drums", "bass", "low")}
    assert find_drops(e, on) == [16]


def test_fill_before_drop_moves_the_drop_to_where_level_arrives():
    # The riser and fill (bars 14-15) climb most of the way, so the drop bar's
    # own 2-bar jump is under DROP_JUMP_DB; the fill's jump is the big one, and
    # the drop moves from it to the bar where the level arrives.
    n = 32
    e = energies(n, drums_off=range(8, 15), loud=range(16, 24))
    e["mix"][14], e["mix"][15] = -4.0, -3.0
    jump = mix_jumps(e["mix"])
    assert jump[16] < DROP_JUMP_DB <= jump[15]
    on = {k: e[k] > -18 for k in ("drums", "bass", "low")}
    assert find_drops(e, on) == [16]


def test_short_break_inside_a_drop_is_not_a_new_drop():
    n = 40
    e = energies(n, drums_off=range(8, 16), loud=list(range(16, 21)) + list(range(23, 32)))
    e["mix"][21:23] = -10.0  # 2-bar break, back to full 7 bars after the drop
    on = {k: e[k] > -18 for k in ("drums", "bass", "low")}
    assert find_drops(e, on) == [16]


def test_jump_a_phrase_after_a_drop_is_a_new_drop():
    n = 40
    e = energies(n, drums_off=range(8, 16), loud=list(range(16, 22)) + list(range(24, 32)))
    e["mix"][22:24] = -10.0  # 2-bar break, back to full PHRASE_BARS after the drop
    on = {k: e[k] > -18 for k in ("drums", "bass", "low")}
    assert find_drops(e, on) == [16, 16 + PHRASE_BARS]


def test_low_lane_ignores_drums_bass_split():
    sr, bar = 8000, 1.0
    t = np.arange(int(4 * bar * sr)) / sr
    low = np.sin(2 * np.pi * 60 * t)[None].repeat(2, 0).astype(np.float32)
    high = np.sin(2 * np.pi * 2000 * t)[None].repeat(2, 0).astype(np.float32)
    silent = np.zeros_like(low)
    downbeats = np.arange(4) * bar
    # 60 Hz only in the last two bars; it sits in drums in one case, bass in the other.
    gate = (t >= 2 * bar).astype(np.float32)
    stems_a = {"drums": low * gate + high, "bass": silent, "other": silent, "vocals": silent}
    stems_b = {"drums": high, "bass": low * gate, "other": silent, "vocals": silent}
    ea = bar_energy(high + low * gate, stems_a, downbeats, 4 * bar, sr)
    eb = bar_energy(high + low * gate, stems_b, downbeats, 4 * bar, sr)
    assert np.allclose(ea["low"], eb["low"], atol=0.5)
    assert ea["low"][3] - ea["low"][0] > 20  # the 2 kHz tone is filtered out


@pytest.mark.skip(reason="level check commented out (decision 0005)")
def test_quiet_riser_is_not_a_drop():
    # Kick back in after a breakdown, but the mix stays well under its loud level.
    n = 32
    e = energies(n, drums_off=range(8, 16))
    e["mix"][:8] = 0.0
    e["mix"][8:16] = -14.0
    e["mix"][16:24] = -7.0
    on = {k: e[k] > -18 for k in ("drums", "bass", "low")}
    assert find_drops(e, on) == []
    e["mix"][16:24] = -1.0
    assert find_drops(e, on) == [16]


@pytest.mark.skip(reason="drum-hold check commented out (decision 0005)")
def test_one_bar_of_drums_is_not_a_drop():
    # Drums come in for two bars (long enough to count as on), then drop out
    # again: the start of a buildup, not a drop.
    n = 32
    e = energies(n, drums_off=list(range(8, 16)) + list(range(18, 24)), loud=range(16, 24))
    on = {k: stable_states(e[k] > -18) for k in ("drums", "bass", "low")}
    assert on["drums"][16:18].all() and not on["drums"][18:20].any()
    assert find_drops(e, on) == []
