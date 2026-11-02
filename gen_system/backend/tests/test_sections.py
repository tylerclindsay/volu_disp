import numpy as np
import pytest

from volu_gen.analysis.models import chunked_sections

SR = 100  # tiny rate: the fake model only needs to know where its window starts


def fake_model(true_bounds: list[float]):
    """Section model stand-in. Its input samples hold absolute time, so it
    can report the true boundaries that fall inside its window, relative to
    the window start — like SongFormer on a slice of the track."""
    def model(x):
        a, b = x[0], x[-1] + 1 / SR
        cuts = [a] + [c for c in true_bounds if a < c < b] + [b]
        return [{"start": s - a, "end": e - a, "label": "verse"} for s, e in zip(cuts, cuts[1:])]
    return model


@pytest.mark.parametrize("dur", [100.0, 396.0, 481.3])
def test_windows_reproduce_whole_track_boundaries(dur):
    bounds = list(np.arange(7.0, dur - 4, 15.0)) + [dur - 3.3]  # off the ownership cuts
    x = np.arange(int(dur * SR)) / SR
    segs = chunked_sections(fake_model(bounds), dur, x, sr=SR)
    starts = [s["start"] for s in segs]
    assert starts == pytest.approx([0.0] + sorted(bounds), abs=0.02)
    assert segs[-1]["end"] == pytest.approx(dur, abs=0.02)
    assert "label" not in segs[0]
