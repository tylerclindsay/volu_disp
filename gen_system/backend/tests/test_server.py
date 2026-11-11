import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from test_methods import analysis as method_analysis
from test_methods import features as method_features

from volu_gen.analysis.features import write_features
from volu_gen.analysis.peaks import compute_peaks, write_peaks
from volu_gen.animfile import FORMAT_VERSION
from volu_gen.generators import DIRECTOR_VERSION, METHODS, DirectorParams
from volu_gen.geometry import parse_display_config
from volu_gen.server import Display, Server, create_app, jobs
from volu_gen.server import db as dbm
from volu_gen.server.jobs import Priority

BENCH = Path(__file__).resolve().parents[3] / "config" / "displays" / "bench.yaml"


@pytest.fixture
def server(tmp_path):
    s = Server(Display.load(BENCH), tmp_path / "data")
    yield s
    s.close()


@pytest.fixture
def client(server):
    with TestClient(create_app(server)) as c:
        yield c


def upload(client, name: str, data: bytes) -> dict:
    r = client.post("/api/tracks", files=[("files", (name, data, "audio/flac"))])
    assert r.status_code == 200, r.text
    return r.json()[0]


def analyzed(server, client, name="a.flac", data=b"audio") -> int:
    """An uploaded track with a finished (synthetic) analysis."""
    track = upload(client, name, data)["track"]
    t = server.library.track_dir(track["key"])
    a = method_analysis()
    a["source"] = {**t.data["source"], "duration": a["source"]["duration"]}
    t.data.update(a)
    t.save()
    write_features(t.features_path, method_features())
    server.library.sync_analysis(track["id"], t, "done")
    return track["id"]


def render_done(server, version_id: int, fps: int, **over):
    """Mark a version rendered, as the worker's result will."""
    cols = {"render_id": "r1", "anim_path": "tracks/x/animations/r1.anim", "fps": fps,
            "pixel_count": server.display.geometry.pixel_count, "geometry_hash": server.display.geometry.hash,
            "format_version": FORMAT_VERSION, "generator_version": DIRECTOR_VERSION, "grid_override": None,
            "rendered_at": dbm.now(), **over}
    with server.db:
        server.db.execute(f"UPDATE versions SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?",
                          (*cols.values(), version_id))


# -- db and display
def test_db_migrates_once(tmp_path):
    db = dbm.connect(tmp_path / "v.db")
    assert db.execute("PRAGMA user_version").fetchone()[0] == len(dbm.MIGRATIONS)
    db.close()
    db = dbm.connect(tmp_path / "v.db")   # already current: no step runs again
    assert db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 0
    db.close()


def test_db_migration_2_records_rendered_method_versions(tmp_path):
    raw = sqlite3.connect(tmp_path / "v.db")
    raw.executescript(f"BEGIN; {dbm.MIGRATIONS[0]}; PRAGMA user_version = 1; COMMIT;")
    plan = json.dumps({"segments": [{"method": "geometry", "method_version": 1},
                                    {"method": "strobe", "method_version": 2}]})
    with raw:
        raw.execute("INSERT INTO tracks (key, name, added_at) VALUES ('k', 'n', 'now')")
        raw.executemany("INSERT INTO versions (track_id, kind, created_at, params, plan, anim_path) "
                        "VALUES (1, ?, 'now', '{}', ?, ?)",
                        [("draft", plan, "tracks/k/animations/r.anim"), ("saved", plan, None)])
    raw.close()
    db = dbm.connect(tmp_path / "v.db")
    assert [dbm.loads(r[0]) for r in db.execute("SELECT method_versions FROM versions ORDER BY id")] == \
        [{"geometry": 1, "strobe": 2}, None]   # from the plan of a rendered row only
    db.close()


def test_display(client, server):
    d = client.get("/api/display").json()
    assert d["valid"] and d["problems"] == []
    assert d["pixel_count"] == 10 * 12 * 4
    assert d["geometry_hash"] == server.display.geometry.hash
    assert d["drive_nodes"][0]["strips"] == 40
    pos = np.frombuffer(client.get("/api/display/positions").content, "<f4").reshape(-1, 3)
    np.testing.assert_allclose(pos, server.display.geometry.positions)


def test_invalid_config_still_serves():
    raw = {"version": 1,
           "geometry": {"size": {"x": 10, "y": 12, "z": 4}, "strip_spacing": 50, "pixel_spacing": 50,
                        "slice_spacing": 25, "odd_slice_offset": {"x": 25, "y": 25}},
           "timing": {"default_fps": 120}, "network": {"udp_port": 7777},
           "drive_nodes": [{"id": 1, "ip": "10.0.0.1", "section": {"x": [0, 9], "y": [0, 11], "z": [0, 2]}}]}
    config = parse_display_config(raw)
    from volu_gen.geometry import Geometry
    from volu_gen.mapping import Mapping, MappingError
    with pytest.raises(MappingError) as e:
        Mapping(config)
    d = Display(config, Geometry.from_config(config), None, tuple(e.value.problems))
    assert not d.summary()["valid"] and "belong to no drive node" in d.summary()["problems"][0]


# -- settings
def test_settings_defaults_and_patch(client):
    s = client.get("/api/settings").json()
    assert s["display_fps"] == 120 and s["preview_fps"] == 60 and s["brightness"] == 1.0
    r = client.patch("/api/settings", json={"display_fps": 50})
    assert r.status_code == 200
    assert r.json()["preview_fps"] == 50     # lowered with the display FPS
    assert client.patch("/api/settings", json={"preview_fps": 51}).status_code == 422
    r = client.patch("/api/settings", json={"display_fps": 29})
    assert r.status_code == 422 and r.json()["detail"][0]["loc"][-1] == "display_fps"   # the UI shows it inline
    assert client.patch("/api/settings", json={"brightness": 1.5}).status_code == 422
    assert client.patch("/api/settings", json={"nope": 1}).status_code == 422
    assert client.get("/api/settings").json()["display_fps"] == 50


def test_display_fps_must_fit_longest_strip(tmp_path):
    # 300-pixel strips fit at most 110 FPS
    cfg = BENCH.read_text().replace("y: 12", "y: 300").replace("y: [0, 11]", "y: [0, 299]")         .replace("default_fps: 120", "default_fps: 100")
    (tmp_path / "long.yaml").write_text(cfg)
    s = Server(Display.load(tmp_path / "long.yaml"), tmp_path / "data")
    with TestClient(create_app(s)) as c:
        assert c.get("/api/display").json()["max_fps"] == 110
        r = c.patch("/api/settings", json={"display_fps": 120})
        assert r.status_code == 422 and "longest strip" in r.text
        assert c.patch("/api/settings", json={"display_fps": 110}).status_code == 200
        assert c.get("/api/settings/fps-impact", params={"fps": 111}).json()["problems"]
    s.close()


def test_settings_persist_and_drop_invalid(tmp_path):
    s = Server(Display.load(BENCH), tmp_path)
    s.settings = s.settings.model_copy(update={"display_fps": 200, "preview_fps": 144, "brightness": 0.5})
    from volu_gen.server import settings as settings_mod
    settings_mod.save(s.db, s.settings)
    with s.db:
        s.db.execute("INSERT INTO settings (key, value) VALUES ('gone', '1')")
    s.close()
    s = Server(Display.load(BENCH), tmp_path)
    assert (s.settings.display_fps, s.settings.preview_fps, s.settings.brightness) == (200, 144, 0.5)
    with s.db:
        s.db.execute("UPDATE settings SET value = '20' WHERE key = 'display_fps'")
    s.close()
    s = Server(Display.load(BENCH), tmp_path)
    assert (s.settings.display_fps, s.settings.preview_fps, s.settings.brightness) == (120, 60, 0.5)
    s.close()


def test_sync_offsets_per_device_and_mode(client):
    assert client.get("/api/sync").json() == {"device": None, "mode": "shared", "display_ms": 0.0,
                                              "preview_ms": 0.0, "calibrated_ms": None}
    client.patch("/api/sync", json={"display_ms": 12.5})
    client.patch("/api/settings", json={"audio_mode": "exclusive"})
    assert client.get("/api/sync").json()["display_ms"] == 0.0
    client.patch("/api/sync", json={"display_ms": -3})
    client.patch("/api/settings", json={"audio_mode": "shared"})
    assert client.get("/api/sync").json()["display_ms"] == 12.5
    assert len(client.get("/api/settings").json()["sync"]) == 2
    assert client.patch("/api/sync", json={"display_ms": 5000}).status_code == 422


# -- library
def test_upload_imports_and_queues_analysis(client, server):
    out = upload(client, "My Track.FLAC", b"audio bytes")
    t = out["track"]
    assert out["created"] and t["name"] == "My Track" and t["analysis"] == "pending"
    assert t["original_path"] == "My Track.FLAC" and t["animation"] == "none"
    assert (server.data_dir / "tracks" / t["key"] / "source.flac").read_bytes() == b"audio bytes"
    assert not list((server.data_dir / "tracks").glob("*.tmp"))
    jobs = client.get("/api/jobs").json()
    assert [(j["kind"], j["track_id"], j["state"], j["priority"]) for j in jobs] == \
        [("analyze", t["id"], "queued", Priority.IMPORT)]
    again = upload(client, "renamed.flac", b"audio bytes")
    assert not again["created"] and again["track"]["id"] == t["id"]
    assert len(client.get("/api/jobs").json()) == 1


def test_scan_folder(client, tmp_path):
    music = tmp_path / "music"
    (music / "sub").mkdir(parents=True)
    (music / "a.wav").write_bytes(b"a")
    (music / "sub" / "b.MP3").write_bytes(b"b")
    (music / "notes.txt").write_bytes(b"c")
    out = client.post("/api/tracks/scan", json={"path": str(music)}).json()
    assert sorted(o["track"]["name"] for o in out) == ["a", "b"]
    assert client.post("/api/tracks/scan", json={"path": str(tmp_path / "nope")}).status_code == 404


def test_remove_keeps_or_deletes_data(client, server):
    a = upload(client, "a.flac", b"a")["track"]
    b = upload(client, "b.flac", b"b")["track"]
    client.post("/api/queue", json={"track_id": a["id"]})
    assert client.get(f"/api/tracks/{a['id']}").json()["data_bytes"] > 0
    assert client.delete(f"/api/tracks/{a['id']}").status_code == 204
    assert client.get("/api/queue").json() == []
    assert [j["track_id"] for j in client.get("/api/jobs").json()] == [b["id"]]
    storage = client.get("/api/storage").json()
    assert [(o["key"], o["name"]) for o in storage["orphans"]] == [(a["key"], "a")]
    # adding the same file again gives the kept directory a new row
    again = upload(client, "a.flac", b"a")
    assert again["created"] and again["track"]["key"] == a["key"]
    client.delete(f"/api/tracks/{again['track']['id']}?delete_data=true")
    assert not (server.data_dir / "tracks" / a["key"]).exists()
    assert client.get("/api/storage").json()["orphans"] == []
    assert client.delete(f"/api/tracks/{a['id']}").status_code == 404


def test_remove_track_with_versions_and_jobs(client, server):
    tid = analyzed(server, client)
    client.post(f"/api/tracks/{tid}/render")
    render_done(server, client.get(f"/api/tracks/{tid}/versions").json()[0]["id"], 120)
    client.post(f"/api/tracks/{tid}/versions", json={})
    assert client.delete(f"/api/tracks/{tid}").status_code == 204
    for table in ("tracks", "versions", "jobs", "play_queue"):
        assert server.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_delete_orphan(client, server):
    a = upload(client, "a.flac", b"a")["track"]
    client.delete(f"/api/tracks/{a['id']}")
    assert client.delete("/api/storage/orphans/nope").status_code == 404
    assert client.delete(f"/api/storage/orphans/{a['key']}").status_code == 204
    assert not (server.data_dir / "tracks" / a["key"]).exists()


def test_analysis_and_peaks(client, server):
    tid = analyzed(server, client)
    a = client.get(f"/api/tracks/{tid}/analysis").json()
    assert a["tempo"]["bpm"] == 120.0
    key = client.get(f"/api/tracks/{tid}").json()["key"]
    assert client.get(f"/api/tracks/{tid}/peaks").status_code == 404
    n = 44100 * 3
    rng = np.random.default_rng(0)
    peaks = compute_peaks({lane: [rng.uniform(-0.5, 0.5, (2, n)).astype(np.float32)]
                           for lane in ["mix", "vocals", "drums", "bass", "other"]}, n)
    write_peaks(server.library.track_dir(key).peaks_path, peaks)
    info = client.get(f"/api/tracks/{tid}/peaks").json()
    assert info["lanes"] == ["mix", "vocals", "drums", "bass", "other"]
    assert [lv["buckets"] for lv in info["levels"]] == [a.shape[2] for a in peaks["levels"]]
    assert info["levels"][0]["rate"] == 300.0
    raw = client.get(f"/api/tracks/{tid}/peaks/2").content
    np.testing.assert_array_equal(np.frombuffer(raw, "<i2").reshape(peaks["levels"][2].shape),
                                  peaks["levels"][2])
    assert client.get(f"/api/tracks/{tid}/peaks/99").status_code == 404
    pending = upload(client, "b.flac", b"b")["track"]["id"]
    partial = client.get(f"/api/tracks/{pending}/analysis").json()   # what exists so far
    assert "source" in partial and "tempo" not in partial
    for path in ("peaks", "peaks/0"):
        assert client.get(f"/api/tracks/{pending}/{path}").status_code == 404
    assert client.get("/api/tracks/999/analysis").status_code == 404


# -- planning and versions
def test_generators_schema(client):
    [g] = client.get("/api/generators").json()
    assert g["id"] == "director" and g["version"] == DIRECTOR_VERSION
    assert "intensity" in g["params_schema"]["properties"]
    assert sorted(m["id"] for m in g["methods"]) == sorted(METHODS)
    assert "neon" in g["palettes"]


def test_plan_needs_analysis(client):
    t = upload(client, "a.flac", b"a")["track"]
    assert client.post(f"/api/tracks/{t['id']}/plan", json={}).status_code == 409
    assert client.get(f"/api/tracks/{t['id']}/plan").json()["plan"] is None


def test_plan_is_deterministic_and_keeps_pins(client, server):
    tid = analyzed(server, client)
    first = client.post(f"/api/tracks/{tid}/plan", json={"params": {"seed": 4}}).json()
    again = client.post(f"/api/tracks/{tid}/plan", json={"params": {"seed": 4}}).json()
    assert first == again
    seg = first["plan"]["segments"][-1]
    field = next(iter(seg["params"]))
    value = seg["params"][field]
    pin = {"start_bar": seg["start_bar"], "end_bar": seg["end_bar"], "method": seg["method"],
           "overrides": {field: value}}
    r = client.post(f"/api/tracks/{tid}/plan", json={"params": {"seed": 9}, "pinned": [pin]}).json()
    kept = [s for s in r["plan"]["segments"] if s["pinned"]]
    assert [(s["start_bar"], s["end_bar"], s["method"], s["overrides"]) for s in kept] == \
        [(pin["start_bar"], pin["end_bar"], pin["method"], pin["overrides"])]
    stored = client.get(f"/api/tracks/{tid}/plan").json()
    assert stored["plan"] == r["plan"] and stored["state"]["params"]["seed"] == 9
    assert stored["state"]["pinned"] == [pin]
    # one draft row, active since the track had no version
    [v] = client.get(f"/api/tracks/{tid}/versions").json()
    assert v["kind"] == "draft" and v["active"] and not v["rendered"]


def test_plan_rejects_bad_state(client, server):
    tid = analyzed(server, client)
    assert client.post(f"/api/tracks/{tid}/plan", json={"params": {"intensity": 2}}).status_code == 422
    seg = client.post(f"/api/tracks/{tid}/plan", json={}).json()["plan"]["segments"][0]
    bad = {"start_bar": seg["start_bar"], "end_bar": seg["end_bar"], "method": seg["method"],
           "overrides": {"no_such_field": 1}}
    plan = client.post(f"/api/tracks/{tid}/plan", json={"pinned": [bad]}).json()["plan"]
    assert all(not s["overrides"] for s in plan["segments"])   # unknown fields are dropped
    numeric = next(k for k, v in seg["params"].items() if isinstance(v, (int, float)) and not isinstance(v, bool))
    bad["overrides"] = {numeric: "not a number"}
    assert client.post(f"/api/tracks/{tid}/plan", json={"pinned": [bad]}).status_code == 422


def test_render_queues_once_and_save_version(client, server):
    tid = analyzed(server, client)
    job = client.post(f"/api/tracks/{tid}/render").json()["job_id"]
    assert client.post(f"/api/tracks/{tid}/render").json()["job_id"] == job   # still queued: same job
    j = next(j for j in client.get("/api/jobs").json() if j["id"] == job)
    assert j["kind"] == "render" and j["priority"] == Priority.STUDIO
    assert j["spec"]["anim_path"] == f"tracks/{j['spec']['key']}/animations/{j['spec']['render_id']}.anim"
    assert client.get("/api/tracks").json()[0]["animation"] == "rendering"
    assert client.post(f"/api/tracks/{tid}/versions", json={}).status_code == 409   # not rendered

    draft = j["version_id"]
    render_done(server, draft, 120)
    with server.db:
        server.db.execute("UPDATE jobs SET state = 'done' WHERE id = ?", (job,))
    assert client.get("/api/tracks").json()[0]["animation"] == "ready"
    saved = client.post(f"/api/tracks/{tid}/versions", json={"name": "v1"}).json()
    assert saved["kind"] == "saved" and saved["playable"] and not saved["active"]
    assert client.put(f"/api/tracks/{tid}/active", json={"version_id": saved["id"]}).status_code == 200
    vs = client.get(f"/api/tracks/{tid}/versions").json()
    assert [(v["kind"], v["active"]) for v in vs] == [("draft", False), ("saved", True)]
    assert client.put(f"/api/tracks/{tid}/active", json={"version_id": 999}).status_code == 404
    other = analyzed(server, client, "b.flac", b"b")
    r = client.put(f"/api/tracks/{other}/active", json={"version_id": saved["id"]})
    assert r.status_code == 404 and "not a version of track" in r.text


def test_stale_and_fps_mismatch(client, server):
    tid = analyzed(server, client)
    client.post(f"/api/tracks/{tid}/plan", json={})
    [v] = client.get(f"/api/tracks/{tid}/versions").json()
    render_done(server, v["id"], 120, geometry_hash="0" * 16)
    assert client.get("/api/tracks").json()[0]["animation"] == "stale"
    [v] = client.get(f"/api/tracks/{tid}/versions").json()
    assert v["stale"] == ["geometry"] and v["playable"]
    render_done(server, v["id"], 120, grid_override=dbm.dumps({"bpm": 128.0, "offset": 0.1}))
    assert client.get(f"/api/tracks/{tid}/versions").json()[0]["stale"] == ["grid"]
    render_done(server, v["id"], 60)
    assert client.get("/api/tracks").json()[0]["animation"] == "fps_mismatch"
    assert not client.get(f"/api/tracks/{tid}/versions").json()[0]["playable"]
    r = client.post("/api/tracks/rerender", json={"which": "fps_mismatch"}).json()
    assert len(r["job_ids"]) == 1
    assert client.get("/api/tracks").json()[0]["animation"] == "rendering"


def test_method_staleness_follows_the_file(client, server):
    """The row's plan can be newer than its file (a Studio edit, or the
    re-plan at the start of a render that then failed): the stale flag
    comes from the method versions the file was rendered with."""
    tid = analyzed(server, client)
    client.post(f"/api/tracks/{tid}/plan", json={})
    [v] = client.get(f"/api/tracks/{tid}/versions").json()
    render_done(server, v["id"], 120, method_versions=dbm.dumps({"geometry": METHODS["geometry"].version + 1}))
    client.post(f"/api/tracks/{tid}/plan", json={})   # the row gets a current plan; the file stays
    assert client.get(f"/api/tracks/{tid}/versions").json()[0]["stale"] == ["method"]
    render_done(server, v["id"], 120, method_versions=dbm.dumps({"geometry": METHODS["geometry"].version}))
    assert client.get(f"/api/tracks/{tid}/versions").json()[0]["stale"] == []


def test_fps_change_rerenders_queued_tracks(client, server):
    a, b = analyzed(server, client, "a.flac", b"a"), analyzed(server, client, "b.flac", b"b")
    for t in (a, b):
        client.post(f"/api/tracks/{t}/plan", json={})
        render_done(server, client.get(f"/api/tracks/{t}/versions").json()[0]["id"], 120)
    client.put("/api/queue", json={"track_ids": [a]})
    impact = client.get("/api/settings/fps-impact", params={"fps": 100}).json()
    assert impact == {"fps": 100, "problems": [], "versions_unplayable": 2, "queue_rerenders": 1,
                      "queue_rerender_seconds": None}   # no render history yet
    client.patch("/api/settings", json={"display_fps": 100})
    renders = [j for j in client.get("/api/jobs").json() if j["kind"] == "render"]
    assert [(j["track_id"], j["priority"], j["spec"]["reason"]) for j in renders] == [(a, Priority.QUEUED, "fps")]
    # queueing a mismatched track re-renders it at play-queue priority
    client.post("/api/queue", json={"track_id": b})
    renders = [j for j in client.get("/api/jobs").json() if j["kind"] == "render"]
    assert sorted(j["track_id"] for j in renders) == [a, b]
    assert all(j["priority"] == Priority.QUEUED for j in renders)


def test_queue_promotes_import_work(client):
    a = upload(client, "a.flac", b"a")["track"]
    upload(client, "b.flac", b"b")
    client.post("/api/queue", json={"track_id": a["id"]})
    prio = {j["track_id"]: j["priority"] for j in client.get("/api/jobs").json()}
    assert prio[a["id"]] == Priority.QUEUED and min(prio.values()) == Priority.QUEUED
    assert [q["track_id"] for q in client.get("/api/queue").json()] == [a["id"]]
    assert client.put("/api/queue", json={"track_ids": [999]}).status_code == 404


def test_queue_reorder_and_remove(client, server):
    a, b, c = (upload(client, f"{n}.flac", n.encode())["track"]["id"] for n in "abc")
    client.put("/api/queue", json={"track_ids": [a, b]})
    with server.db:   # put a's job back, to see that a (already queued) isn't promoted again
        server.db.execute("UPDATE jobs SET priority = ? WHERE track_id = ?", (int(Priority.IMPORT), a))
    q = client.put("/api/queue", json={"track_ids": [c, a]}).json()
    assert [x["track_id"] for x in q] == [c, a] and q[0]["animation"] == "none"
    prio = {j["track_id"]: j["priority"] for j in client.get("/api/jobs").json()}
    assert prio == {a: Priority.IMPORT, b: Priority.QUEUED, c: Priority.QUEUED}   # only newcomers move up
    assert client.post("/api/queue", json={"track_id": 999}).status_code == 404
    assert [x["track_id"] for x in client.post("/api/queue", json={"track_id": b}).json()] == [c, a, b]
    assert client.put("/api/queue", json={"track_ids": []}).json() == []
    assert [t["id"] for t in client.get("/api/tracks").json()] == [a, b, c]


def test_grid_override_queues_regrid(client, server):
    t = upload(client, "a.flac", b"x")["track"]
    assert client.put(f"/api/tracks/{t['id']}/grid", json={"bpm": 128, "offset": 0.1}).status_code == 409
    tid = analyzed(server, client, "b.flac", b"y")
    r = client.put(f"/api/tracks/{tid}/grid", json={"bpm": 500, "offset": 0.1})
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", "bpm"]
    j1 = client.put(f"/api/tracks/{tid}/grid", json={"bpm": 128, "offset": 0.1}).json()["job_id"]
    j2 = client.delete(f"/api/tracks/{tid}/grid").json()["job_id"]
    regrids = [j for j in client.get("/api/jobs", params={"state": "queued"}).json() if j["kind"] == "regrid"]
    assert [j["id"] for j in regrids] == [j2] and j1 != j2      # only the latest override is queued
    assert regrids[0]["spec"]["bpm"] is None and regrids[0]["priority"] == Priority.STUDIO


def test_rerender_active_version(client, server):
    """Row action and bulk actions: re-render the active version in place,
    as import work, or at play-queue priority for a queued track."""
    t = upload(client, "a.flac", b"a")["track"]
    assert client.post(f"/api/tracks/{t['id']}/rerender").status_code == 409   # no active version
    assert client.post("/api/tracks/999/rerender").status_code == 404
    tid = analyzed(server, client, "b.flac", b"b")
    client.post(f"/api/tracks/{tid}/plan", json={})
    [v] = client.get(f"/api/tracks/{tid}/versions").json()
    render_done(server, v["id"], 120, generator_version=DIRECTOR_VERSION + 1)
    assert client.get(f"/api/tracks/{tid}").json()["animation"] == "stale"
    r = client.post("/api/tracks/rerender", json={"which": "stale"}).json()
    [job] = [j for j in client.get("/api/jobs").json() if j["id"] in r["job_ids"]]
    assert (job["version_id"], job["priority"], job["spec"]["reason"]) == (v["id"], Priority.IMPORT, "stale")
    assert client.post("/api/tracks/rerender", json={"which": "stale"}).json()["job_ids"] == []   # rendering
    client.delete(f"/api/jobs/{job['id']}")
    client.post("/api/queue", json={"track_id": tid})
    job = client.post(f"/api/tracks/{tid}/rerender").json()["job_id"]
    j = next(j for j in client.get("/api/jobs").json() if j["id"] == job)
    assert (j["version_id"], j["priority"]) == (v["id"], Priority.QUEUED)


def test_job_order(server):
    """Highest priority first; within a priority a default render goes
    first (each import gets its default render before the next analyze),
    then FIFO."""
    lib, db = server.library, server.db
    ids = []
    for n in "abc":
        path = server.data_dir / f"{n}.flac"
        path.write_bytes(n.encode())
        ids.append(lib.add(lib.import_file(path))[0])
    vid = lib.set_draft_plan(ids[0], DirectorParams(), {"segments": []})
    analyze = [r["id"] for r in db.execute("SELECT id FROM jobs ORDER BY id")]
    assert jobs.next_job(db)["id"] == analyze[0]
    with db:
        db.execute("UPDATE jobs SET state = 'done' WHERE id = ?", (analyze[0],))
    default =jobs.queue_render(db, vid, jobs.DEFAULT, Priority.IMPORT)
    assert jobs.next_job(db)["id"] == default                 # ahead of the older analyze of b
    jobs.promote_track(db, ids[2])
    assert jobs.next_job(db)["id"] == analyze[2]              # c joined the play queue
    regrid = jobs.queue_regrid(db, ids[0], lib.track(ids[0])["key"], 128.0, 0.0)
    assert jobs.next_job(db)["id"] == regrid
    with db:
        db.execute("UPDATE jobs SET state = 'done'")
    assert jobs.next_job(db) is None


def test_fps_impact_estimates_rerender_time(client, server):
    tid = analyzed(server, client)
    client.post(f"/api/tracks/{tid}/plan", json={})
    [v] = client.get(f"/api/tracks/{tid}/versions").json()
    render_done(server, v["id"], 120)
    client.put("/api/queue", json={"track_ids": [tid]})
    assert client.get("/api/settings/fps-impact", params={"fps": 60}).json()["queue_rerender_seconds"] is None
    p = server.display.geometry.pixel_count
    # two past renders: 1000 frames in 10 s, 3000 in 50 s → 60 s / 4000 frames
    for frames, seconds in ((1000, 10), (3000, 50)):
        with server.db:
            server.db.execute(
                "INSERT INTO jobs (kind, track_id, version_id, spec, priority, state, created_at, started_at, "
                "finished_at, result) VALUES ('render', ?, ?, '{}', 0, 'done', '', ?, ?, ?)",
                (tid, v["id"], "2026-01-01T00:00:00+00:00", f"2026-01-01T00:00:{seconds:02d}+00:00",
                 dbm.dumps({"frame_count": frames, "pixel_count": p})))
    duration = client.get(f"/api/tracks/{tid}").json()["duration"]
    impact = client.get("/api/settings/fps-impact", params={"fps": 60}).json()
    assert impact["queue_rerenders"] == 1
    assert impact["queue_rerender_seconds"] == pytest.approx(60 / 4000 * 60 * duration, abs=0.1)


def test_jobs_cancel_and_retry(client, server):
    t = upload(client, "a.flac", b"a")["track"]
    [j] = client.get("/api/jobs").json()
    assert client.post(f"/api/jobs/{j['id']}/retry").status_code == 409   # still queued
    assert client.delete(f"/api/jobs/{j['id']}").status_code == 204
    assert client.delete(f"/api/jobs/{j['id']}").status_code == 409
    new = client.post(f"/api/jobs/{j['id']}/retry").json()["job_id"]
    states = {x["id"]: x["state"] for x in client.get("/api/jobs").json()}
    assert states == {j["id"]: "cancelled", new: "queued"}
    assert [x["id"] for x in client.get("/api/jobs", params={"state": "queued"}).json()] == [new]
    assert t["id"] == j["track_id"]
    assert client.post(f"/api/jobs/{j['id']}/retry").json()["job_id"] == new   # still open: the same job
    assert client.post("/api/jobs/999/retry").status_code == 404
    assert len(client.get("/api/jobs", params={"limit": 1}).json()) == 1


def test_retry_regrid_replaces_a_queued_one(client, server):
    tid = analyzed(server, client)
    old = client.put(f"/api/tracks/{tid}/grid", json={"bpm": 128, "offset": 0.1}).json()["job_id"]
    newer = client.delete(f"/api/tracks/{tid}/grid").json()["job_id"]
    again = client.post(f"/api/jobs/{old}/retry").json()["job_id"]
    states = {j["id"]: j["state"] for j in client.get("/api/jobs").json() if j["kind"] == "regrid"}
    assert states == {old: "cancelled", newer: "cancelled", again: "queued"}   # only the latest override


# -- presets
def test_presets(client):
    p = client.post("/api/presets", json={"name": "calm", "params": {"intensity": 0.5}})
    assert p.status_code == 201
    pid = p.json()["id"]
    assert client.post("/api/presets", json={"name": "calm", "params": {}}).status_code == 409
    assert client.post("/api/presets", json={"name": "x", "params": {"intensity": 9}}).status_code == 422
    r = client.post("/api/presets", json={"name": "x", "generator": "other", "params": {}})
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", "generator"]
    r = client.put(f"/api/presets/{pid}", json={"name": "calmer", "params": {"intensity": 0.3}}).json()
    assert r["name"] == "calmer" and r["params"]["intensity"] == 0.3
    assert [x["name"] for x in client.get("/api/presets").json()] == ["calmer"]
    assert client.get("/api/presets", params={"generator": "other"}).json() == []
    other = client.post("/api/presets", json={"name": "loud", "params": {}}).json()["id"]
    assert client.put(f"/api/presets/{other}", json={"name": "calmer", "params": {}}).status_code == 409
    assert client.put("/api/presets/999", json={"name": "y", "params": {}}).status_code == 404
    assert client.delete(f"/api/presets/{pid}").status_code == 204
    assert client.delete(f"/api/presets/{pid}").status_code == 404


def test_cli_rejects_missing_config(tmp_path):
    from volu_gen.server.cli import main
    with pytest.raises(SystemExit) as e:
        main(["serve", "--display", str(tmp_path / "nope.yaml"), "--data", str(tmp_path)])
    assert "volu serve" in str(e.value)


def test_import_original_name(tmp_path):
    from volu_gen.analysis import import_track
    tmp = tmp_path / ".upload-1.tmp"
    tmp.write_bytes(b"x")
    t = import_track(tmp, tmp_path / "data", "Song.MP3")
    assert t.source_path.name == "source.mp3" and t.name == "Song"
    assert json.loads(t.json_path.read_text())["source"]["original_path"] == "Song.MP3"
