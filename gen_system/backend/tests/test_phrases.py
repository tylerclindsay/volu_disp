import numpy as np

from volu_gen.analysis.mood import library_z
from volu_gen.analysis.phrases import compute_phrases, stable_states

BAR_S = 2.0


def energies(n_bars: int, drums_off=(), bass_off=(), loud=()):
    """Per-bar dB: everything at -6 dB, stems off at -40, `loud` bars at 0."""
    e = {k: np.full(n_bars, -6.0) for k in ("mix", "drums", "bass", "other", "vocals")}
    for i in drums_off:
        e["drums"][i] = -40.0
        e["mix"][i] = -12.0
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
    out = compute_phrases(energies(n, drums_off=range(12, 20)), downbeats, n * BAR_S,
                          [{"start": 0.0, "end": 64.0}])
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
