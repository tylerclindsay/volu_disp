import hashlib
import json

from volu_gen.analysis.track import SCHEMA_VERSION, TRACKS_DIR, TrackDir, import_track, stored_tracks


def write_file(path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_import_copies_source(tmp_path):
    src = write_file(tmp_path / "music" / "My Track.FLAC", b"audio bytes")
    t = import_track(src, tmp_path / "data")
    sha = hashlib.sha256(b"audio bytes").hexdigest()
    assert t.dir == tmp_path / "data" / TRACKS_DIR / sha[:16]
    assert t.source_path == t.dir / "source.flac"
    assert t.source_path.read_bytes() == b"audio bytes"
    assert t.name == "My Track"
    assert t.data["source"] == {"sha256": sha, "original_path": str(src), "name": "My Track"}
    # The original can go away; the track still has its audio.
    src.unlink()
    assert TrackDir(t.dir).source_path.read_bytes() == b"audio bytes"
    assert not list((tmp_path / "data" / TRACKS_DIR).glob(".import-*"))


def test_reimport_same_bytes_is_same_track(tmp_path):
    a = import_track(write_file(tmp_path / "a.wav", b"same"), tmp_path / "data")
    before = a.json_path.stat().st_mtime_ns
    b = import_track(write_file(tmp_path / "renamed.wav", b"same"), tmp_path / "data")
    assert a.dir == b.dir
    assert a.json_path.stat().st_mtime_ns == before  # existing track not written
    assert b.name == "a"  # first import's name and path are kept
    assert [t.dir for t in stored_tracks(tmp_path / "data")] == [a.dir]
    assert not list((tmp_path / "data" / TRACKS_DIR).glob(".import-*"))


def test_changed_bytes_are_a_new_track(tmp_path):
    a = import_track(write_file(tmp_path / "a.wav", b"v1"), tmp_path / "data")
    b = import_track(write_file(tmp_path / "a.wav", b"v2"), tmp_path / "data")
    assert a.dir != b.dir
    assert len(stored_tracks(tmp_path / "data")) == 2


def test_migrates_in_place_reference(tmp_path):
    """A track dir from before copy-on-import: analysis.json points at the
    original file, there is no source copy yet."""
    src = write_file(tmp_path / "music" / "old.mp3", b"old audio")
    sha = hashlib.sha256(b"old audio").hexdigest()
    d = tmp_path / "data" / TRACKS_DIR / sha[:16]
    d.mkdir(parents=True)
    old = {"version": SCHEMA_VERSION, "source": {"sha256": sha, "path": str(src), "duration": 1.5},
           "models": {"beats": "x"}, "tempo": {"bpm": 120.0}}
    (d / "analysis.json").write_text(json.dumps(old))
    assert stored_tracks(tmp_path / "data") == []  # not imported yet

    t = import_track(src, tmp_path / "data")
    assert t.dir == d
    assert t.source_path == d / "source.mp3"
    assert t.data["source"] == {"sha256": sha, "original_path": str(src), "duration": 1.5, "name": "old"}
    assert t.data["tempo"] == {"bpm": 120.0}  # analysis kept


def test_schema_reset_keeps_source(tmp_path):
    t = import_track(write_file(tmp_path / "a.wav", b"x"), tmp_path / "data")
    t.data["version"] = -1
    t.save()
    again = TrackDir(t.dir)
    assert again.data["version"] == SCHEMA_VERSION
    assert again.data["source"]["name"] == "a"
