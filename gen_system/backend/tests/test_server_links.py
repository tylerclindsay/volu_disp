"""The server's links (docs/architecture.md → API server, IPC, Main flows):
job dispatch to a real worker (a fake analysis, real regrid and render
jobs), the engine link against a fake engine, the WebSocket, cleanup,
node detection and child processes. Real ZeroMQ sockets on free ports."""

import asyncio
import queue
import subprocess
import sys
import threading
import time

import numpy as np
import pytest
import zmq
from fastapi.testclient import TestClient
from test_ipc import free_port
from test_methods import analysis as method_analysis
from test_methods import features as method_features
from test_server import BENCH, analyzed, render_done, upload

from volu_gen.analysis.features import write_features
from volu_gen.analysis.grid import grid_times
from volu_gen.analysis.track import TrackDir
from volu_gen.generators import DIRECTOR_VERSION, DirectorParams, RenderCancelled
from volu_gen.ipc import (TO_ENGINE, AnalyzeResult, ClearNext, Configure, EngineHello, Hello, Load, Pause, Play,
                          Released, SlotState, Status, Stop, SwapAnim, Test, TestOff, TrackEnded, TrackStarted,
                          bind_pub, bind_router, decode, encode_preview, send_to)
from volu_gen.server import Display, Runtime, Server, cleanup, create_app
from volu_gen.server import db as dbm
from volu_gen.server.children import Child, kill_pid, probe_hello
from volu_gen.server.jobs import Priority
from volu_gen.server.nodes import NodeWatch, ping
from volu_gen.worker import Worker, run_regrid, run_render

FPS = 30   # display FPS in these tests: renders of the 20 s synthetic track stay small


def wait_until(cond, timeout=20.0, what="condition"):
    deadline = time.monotonic() + timeout
    while not (v := cond()):
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)
    return v


def fake_analyze(spec, env):
    """The analysis models' output, from the synthetic track of
    test_methods (with the fitted grid a regrid needs)."""
    t = TrackDir(env.data_dir / "tracks" / spec.key)
    a = method_analysis()
    beats, downbeats = grid_times(a["tempo"]["bpm"], a["tempo"]["offset"], a["source"]["duration"])
    a["tempo"]["fit"] = {"used": True, "bpm": a["tempo"]["bpm"], "offset": a["tempo"]["offset"]}
    a["tempo"]["raw"] = {"beats": beats.tolist(), "downbeats": downbeats.tolist()}
    a["source"] = {**t.data["source"], "duration": a["source"]["duration"]}
    t.data.update(a)
    t.save()
    write_features(t.features_path, method_features())
    return AnalyzeResult(key=spec.key, duration=a["source"]["duration"], bpm=a["tempo"]["bpm"],
                         tempo_mode=a["tempo"].get("mode"), grid_override=None)


RUNNERS = {"analyze": fake_analyze, "regrid": run_regrid, "render": run_render}


class Gate:
    """Wraps a runner: the job waits until `go` (or its cancel)."""

    def __init__(self, inner=run_render):
        self.inner = inner
        self.go = threading.Event()
        self.waiting = threading.Event()

    def __call__(self, spec, env):
        self.waiting.set()
        while not self.go.wait(0.01):
            if env.cancelled():
                raise RenderCancelled(spec.render_id)
        return self.inner(spec, env)


class FakeEngine:
    """Binds the engine's sockets and acts like an engine as far as the
    server can see: answers hello with its slots, keeps slots and play
    state from the commands, and sends status as a heartbeat. Records
    every message."""

    def __init__(self, control_port: int, preview_port: int, instance="e1"):
        self.ctx = zmq.Context()
        self.router = bind_router(self.ctx, control_port)
        self.pub = bind_pub(self.ctx, preview_port)
        self.instance = instance
        self.got: list = []
        self.slots: dict[str, Load] = {}
        self.state = "stopped"
        self.position = 0.0
        self._out: queue.SimpleQueue = queue.SimpleQueue()
        self._peer = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        last = 0.0
        while not self._stop.is_set():
            if self.router.poll(10):
                peer, data = self.router.recv_multipart()
                msg = decode(TO_ENGINE, data)
                self._peer = peer
                self._apply(msg)
                self.got.append(msg)
                if isinstance(msg, Hello):
                    send_to(self.router, peer, EngineHello(
                        instance_id=self.instance, pid=4242, state=self.state, position=self.position,
                        slots=[SlotState(slot=k, track_id=v.track_id, audio_path=v.audio_path, anim_path=v.anim_path)
                               for k, v in self.slots.items()]))
            while True:
                try:
                    item = self._out.get_nowait()
                except queue.Empty:
                    break
                if isinstance(item, bytes):
                    self.pub.send(item)
                elif self._peer is not None:
                    send_to(self.router, self._peer, item)
            if self._peer is not None and time.monotonic() - last > 0.05:
                cur = self.slots.get("current")
                send_to(self.router, self._peer, Status(state=self.state, track_id=cur.track_id if cur else None,
                                                        position=self.position if cur else None))
                last = time.monotonic()

    def _apply(self, msg):
        if isinstance(msg, Load):
            self.slots[msg.slot] = msg
            if msg.slot == "current":
                self.position = msg.start
                if self.state == "stopped":
                    self.state = "paused"
        elif isinstance(msg, ClearNext):
            self.slots.pop("next", None)
        elif isinstance(msg, Stop):
            self.slots.clear()
            self.state = "stopped"
        elif isinstance(msg, Play) and "current" in self.slots:
            self.state = "playing"
        elif isinstance(msg, Pause) and self.state == "playing":
            self.state = "paused"

    def send(self, item):
        self._out.put(item)

    def of(self, cls) -> list:
        return [m for m in list(self.got) if isinstance(m, cls)]

    def wait_for(self, pred, timeout=10.0, what="an engine message"):
        return wait_until(lambda: next((m for m in list(self.got) if pred(m)), None), timeout, what)

    def mark(self) -> int:
        return len(self.got)

    def since(self, mark: int, n: int = 0) -> list:
        """The messages after `mark`, once at least n arrived (an HTTP call
        returns when the server sent them, not when this thread has them)."""
        wait_until(lambda: len(self.got) >= mark + n, 10.0, f"{n} engine messages")
        return list(self.got)[mark:]

    def close(self):
        self._stop.set()
        self._thread.join(5)
        self.router.close()
        self.pub.close()
        self.ctx.term()


class Rig:
    """A server with links (connect-only: no child processes), plus a
    worker thread and a fake engine on its ports when asked."""

    def __init__(self, tmp_path):
        self.data = tmp_path / "data"
        self.runtime = Runtime(engine_control_port=free_port(), engine_preview_port=free_port(),
                               worker_port=free_port(), spawn=False, ping=False)
        self.display = Display.load(BENCH)
        self.server = self.client = None
        self.worker = self.worker_thread = None
        self.engine = None
        self.log: list[str] = []

    def start_server(self, fps=FPS):
        self.server = Server(self.display, self.data, runtime=self.runtime)
        self.server.log = self.log.append
        self.client = TestClient(create_app(self.server), backend_options={"loop_factory": asyncio.SelectorEventLoop})
        self.client.__enter__()
        if self.server.settings.display_fps != fps:
            assert self.client.patch("/api/settings", json={"display_fps": fps}).status_code == 200
        return self.client

    def stop_server(self):
        self.client.__exit__(None, None, None)
        self.server.close()
        self.server = self.client = None

    def start_worker(self, runners=None):
        self.worker = Worker(self.data, self.display.geometry, self.runtime.worker_port, server_timeout=0,
                             log=lambda line: None, runners={**RUNNERS, **(runners or {})})
        self.worker_thread = threading.Thread(target=self.worker.run, daemon=True)
        self.worker_thread.start()
        wait_until(lambda: self.server is None or self.state()["worker"]["connected"], what="the worker")

    def stop_worker(self):
        self.worker.stop()
        self.worker_thread.join(5)
        self.worker.close()
        self.worker = None

    def start_engine(self, instance="e1"):
        self.engine = FakeEngine(self.runtime.engine_control_port, self.runtime.engine_preview_port, instance)
        if self.server is not None:
            wait_until(lambda: self.state()["engine"]["connected"], what="the engine")
        return self.engine

    def stop_engine(self):
        self.engine.close()
        self.engine = None

    def state(self) -> dict:
        return self.client.get("/api/state").json()

    def call(self, fn, *args):
        """Run fn on the server's event loop."""
        return self.client.portal.call(fn, *args)

    def job(self, job_id) -> dict:
        return next(j for j in self.client.get("/api/jobs").json() if j["id"] == job_id)

    def wait_job(self, job_id, state="done", timeout=60.0) -> dict:
        j = wait_until(lambda: (j := self.job(job_id))["state"] in (state, "failed") and j, timeout,
                       f"job {job_id} to be {state}")
        assert j["state"] == state, j
        return j

    def ready_track(self, name="a.flac", data=b"a") -> int:
        """An imported track, analyzed and rendered through the worker."""
        tid = upload(self.client, name, data)["track"]["id"]
        wait_until(lambda: self.client.get(f"/api/tracks/{tid}").json()["animation"] == "ready", 60,
                   f"track {tid} to be ready")
        return tid

    def close(self):
        if self.client is not None:
            self.stop_server()
        assert not [line for line in self.log if "internal task failed" in line], self.log
        if self.worker is not None:
            self.stop_worker()
        if self.engine is not None:
            self.stop_engine()


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


def rendered(rig, name: str, data: bytes) -> tuple[int, int]:
    """A track with a rendered draft (no worker: the row says so)."""
    tid = analyzed(rig.server, rig.client, name, data)
    rig.client.post(f"/api/tracks/{tid}/plan", json={})
    [v] = rig.client.get(f"/api/tracks/{tid}/versions").json()
    render_done(rig.server, v["id"], FPS, render_id=f"r{tid}", anim_path=anim(rig, tid, f"r{tid}"))
    return tid, v["id"]


def anim(rig, track_id: int, render_id: str) -> str:
    return f"tracks/{rig.client.get(f'/api/tracks/{track_id}').json()['key']}/animations/{render_id}.anim"


# -- job dispatch (Main flows 1, 3, 4, 6)
def test_import_runs_to_a_playable_draft(rig):
    rig.start_server()
    rig.start_worker()
    tid = rig.ready_track()
    js = sorted(rig.client.get("/api/jobs").json(), key=lambda j: j["id"])
    assert [(j["kind"], j["state"]) for j in js] == [("analyze", "done"), ("render", "done")]
    assert js[1]["spec"]["reason"] == "default" and js[0]["result"]["bpm"] == 120.0
    t = rig.client.get(f"/api/tracks/{tid}").json()
    assert t["analysis"] == "done" and t["bpm"] == 120.0
    [v] = rig.client.get(f"/api/tracks/{tid}/versions", params={"plan": True}).json()
    assert v["kind"] == "draft" and v["active"] and v["playable"] and v["fps"] == FPS and v["stale"] == []
    assert v["plan"]["segments"][-1]["end"] == pytest.approx(t["duration"])   # planned and timed at render start
    assert (rig.data / js[1]["result"]["anim_path"]).exists()


def test_regrid_replans_and_rerender_clears_stale(rig):
    rig.start_server()
    rig.start_worker()
    tid = rig.ready_track()
    before = rig.client.get(f"/api/tracks/{tid}/plan").json()["plan"]
    job = rig.client.put(f"/api/tracks/{tid}/grid", json={"bpm": 128, "offset": 0.1}).json()["job_id"]
    rig.wait_job(job)
    t = rig.client.get(f"/api/tracks/{tid}").json()
    assert t["grid_override"] == {"bpm": 128.0, "offset": 0.1} and t["animation"] == "stale"
    # re-planned on the new grid (just after the job shows done: the re-plan runs off the event loop)
    wait_until(lambda: rig.client.get(f"/api/tracks/{tid}/plan").json()["plan"] != before, what="the re-plan")
    rig.wait_job(rig.client.post(f"/api/tracks/{tid}/render").json()["job_id"])
    assert rig.client.get(f"/api/tracks/{tid}").json()["animation"] == "ready"


def test_stale_plan_is_replanned_before_render(rig):
    rig.start_server()
    rig.start_worker()
    tid = rig.ready_track()
    d = rig.server.library.draft(tid)
    plan = dbm.loads(d["plan"])
    plan["director"]["version"] = DIRECTOR_VERSION + 1
    for s in plan["segments"]:
        s["start"] = s["end"] = 0.0   # times are recomputed too
    with rig.server.db:
        rig.server.db.execute("UPDATE versions SET plan = ? WHERE id = ?", (dbm.dumps(plan), d["id"]))
    rig.wait_job(rig.client.post(f"/api/tracks/{tid}/render").json()["job_id"])
    stored = dbm.loads(rig.server.library.draft(tid)["plan"])
    assert stored["director"]["version"] == DIRECTOR_VERSION and stored["segments"][-1]["end"] > 0


def test_planning_failure_fails_the_render(rig):
    rig.start_server()
    rig.start_worker()
    tid = rig.ready_track()
    t = rig.server.library.track_dir(rig.client.get(f"/api/tracks/{tid}").json()["key"])
    del t.data["phrases"]   # an analysis the director can't plan from
    t.save()
    job = rig.client.post(f"/api/tracks/{tid}/rerender").json()["job_id"]
    j = rig.wait_job(job, "failed")
    assert "planning failed" in j["error"] and "not finished" in j["error"] and j["traceback"]


def test_cancel_running_render_and_fps_change(rig):
    rig.start_server()
    gate = Gate()
    rig.start_worker({"render": gate})
    tid = upload(rig.client, "a.flac", b"a")["track"]["id"]
    wait_until(gate.waiting.is_set, what="the default render")
    [default] = [j for j in rig.client.get("/api/jobs").json() if j["kind"] == "render"]
    assert rig.client.delete(f"/api/jobs/{default['id']}").status_code == 204
    rig.wait_job(default["id"], "cancelled")

    gate.waiting.clear()
    studio = rig.client.post(f"/api/tracks/{tid}/render").json()["job_id"]
    wait_until(gate.waiting.is_set, what="the Studio render")
    assert rig.job(studio)["state"] == "running"
    assert rig.client.patch("/api/settings", json={"display_fps": 60}).status_code == 200
    rig.wait_job(studio, "cancelled")
    [again] = [j for j in rig.client.get("/api/jobs").json() if j["kind"] == "render" and j["id"] > studio]
    assert again["version_id"] == rig.job(studio)["version_id"] and again["spec"]["reason"] == "studio"
    gate.go.set()
    assert rig.wait_job(again["id"])["result"]["fps"] == 60
    assert rig.client.get(f"/api/tracks/{tid}").json()["animation"] == "ready"


def test_queueing_a_track_during_its_analysis_raises_its_default_render(rig):
    rig.start_server()
    gate = Gate(fake_analyze)
    rig.start_worker({"analyze": gate})
    tid = upload(rig.client, "a.flac", b"a")["track"]["id"]
    wait_until(gate.waiting.is_set, what="the analysis")
    rig.client.post("/api/queue", json={"track_id": tid})
    gate.go.set()
    render = wait_until(lambda: next((j for j in rig.client.get("/api/jobs").json() if j["kind"] == "render"),
                                     None), what="the default render")
    assert (render["spec"]["reason"], render["priority"]) == ("default", Priority.QUEUED)
    rig.wait_job(render["id"])


def test_cancel_analysis_needs_a_managed_worker(rig):
    rig.start_server()
    gate = Gate(fake_analyze)
    rig.start_worker({"analyze": gate})
    upload(rig.client, "a.flac", b"a")
    wait_until(gate.waiting.is_set)
    [j] = rig.client.get("/api/jobs").json()
    r = rig.client.delete(f"/api/jobs/{j['id']}")
    assert r.status_code == 409 and "started" in r.text   # connect-only: the server can't kill this worker
    gate.go.set()
    rig.wait_job(j["id"])


def test_worker_restart_resubmits_the_running_job(rig):
    rig.start_server()
    gate = Gate(fake_analyze)
    rig.start_worker({"analyze": gate})
    upload(rig.client, "a.flac", b"a")
    wait_until(gate.waiting.is_set)
    [j] = rig.client.get("/api/jobs").json()
    rig.stop_worker()          # its job thread stays stuck in the gate; the new worker never heard of the job
    wait_until(lambda: not rig.state()["worker"]["connected"], what="the disconnect")
    rig.start_worker()
    rig.wait_job(j["id"])
    assert rig.client.get(f"/api/tracks/{j['track_id']}").json()["analysis"] == "done"
    gate.go.set()


def test_removing_a_track_cancels_its_render(rig):
    rig.start_server()
    gate = Gate()
    rig.start_worker({"render": gate})
    tid = upload(rig.client, "a.flac", b"a")["track"]["id"]
    wait_until(gate.waiting.is_set)
    assert rig.client.delete(f"/api/tracks/{tid}", params={"delete_data": True}).status_code == 204
    assert not list((rig.data / "tracks").iterdir())
    assert rig.client.get("/api/jobs").json() == [] and rig.state()["worker"]["job_id"] is None


# -- the engine link (Main flows 2, 3, 5, 7; Failure handling)
def test_engine_gets_configure_and_live_settings(rig):
    rig.start_server()
    eng = rig.start_engine()
    c = eng.wait_for(lambda m: isinstance(m, Configure) and m.display_fps == FPS)
    assert c.geometry_hash == rig.display.geometry.hash and not c.armed and c.online_nodes == []
    rig.client.patch("/api/settings", json={"brightness": 0.5})
    eng.wait_for(lambda m: isinstance(m, Configure) and m.brightness == 0.5)
    rig.client.patch("/api/sync", json={"display_ms": 12.0})
    eng.wait_for(lambda m: isinstance(m, Configure) and m.display_offset_ms == 12.0)
    out = rig.client.put("/api/output/armed", json={"armed": True}).json()
    assert out["armed"] and out["armed_override"] is True
    eng.wait_for(lambda m: isinstance(m, Configure) and m.armed)
    assert rig.client.post("/api/display/test", json={"pattern": "sweep", "params": {"axis": "x"}}).status_code == 200
    eng.wait_for(lambda m: isinstance(m, Test) and m.params == {"axis": "x"})
    assert rig.client.delete("/api/display/test").status_code == 204
    eng.wait_for(lambda m: isinstance(m, TestOff))
    rig.stop_engine()
    wait_until(lambda: not rig.state()["engine"]["connected"])
    assert rig.client.post("/api/display/test", json={"pattern": "sweep"}).status_code == 409
    r = rig.client.post("/api/player/play")
    assert r.status_code == 409 and "not connected" in r.text
    assert rig.client.put("/api/player/mode", json={"mode": "studio"}).json()["mode"] == "studio"   # kept for later


def test_play_the_queue(rig):
    rig.start_server()
    a, _ = rendered(rig, "a.flac", b"a")
    x = upload(rig.client, "x.flac", b"x")["track"]["id"]   # not rendered: skipped, keeps its place
    b, _ = rendered(rig, "b.flac", b"b")
    eng = rig.start_engine()
    assert rig.client.post("/api/player/play").status_code == 409   # empty queue
    rig.client.put("/api/queue", json={"track_ids": [a, x, b]})
    m = eng.mark()
    p = rig.client.post("/api/player/play").json()
    assert p["current_track_id"] == a and p["slots"]["next"]["track_id"] == b
    sent = [type(x).__name__ for x in eng.since(m, 3)]
    assert sent == ["Load", "Load", "Play"]
    assert [(x.slot, x.track_id) for x in eng.since(m)[:2]] == [("current", a), ("next", b)]
    assert [q["track_id"] for q in rig.client.get("/api/queue").json()] == [x, b]

    eng.send(TrackEnded(track_id=a))
    eng.send(TrackStarted(track_id=b))
    wait_until(lambda: rig.state()["player"]["current_track_id"] == b, what="track b to start")
    assert [q["track_id"] for q in rig.client.get("/api/queue").json()] == [x]

    # x becomes playable while b plays: it is preloaded as next
    vid = rig.server.library.ensure_draft(x, DirectorParams())
    render_done(rig.server, vid, FPS, render_id="rx", anim_path="tracks/xxxxxxxxxxxxxxxx/animations/r.anim")
    rig.call(rig.server.player.on_render_done, vid)
    eng.wait_for(lambda m: isinstance(m, Load) and m.slot == "next" and m.track_id == x)

    rig.client.post("/api/player/next")
    eng.wait_for(lambda msg: isinstance(msg, Load) and msg.slot == "current" and msg.track_id == x)
    assert rig.client.get("/api/queue").json() == []
    eng.send(TrackEnded(track_id=x))
    wait_until(lambda: rig.state()["player"]["current_track_id"] is None, what="the end of the queue")


def test_render_swaps_the_loaded_animation_and_cleans_up(rig):
    rig.start_server()
    rig.start_worker()
    tid = rig.ready_track()
    eng = rig.start_engine()
    rig.client.put("/api/queue", json={"track_ids": [tid]})
    rig.client.post("/api/player/play")
    old = eng.wait_for(lambda m: isinstance(m, Load) and m.slot == "current").anim_path
    rig.wait_job(rig.client.post(f"/api/tracks/{tid}/render").json()["job_id"])
    swap = eng.wait_for(lambda m: isinstance(m, SwapAnim))
    assert swap.anim_path != old and (rig.data / swap.anim_path).exists()
    assert (rig.data / old).exists()   # the engine still has it open
    eng.send(Released(anim_path=old))
    wait_until(lambda: not (rig.data / old).exists(), what="the released file's deletion")
    assert (rig.data / swap.anim_path).exists()


def test_studio_mode(rig):
    rig.start_server()
    a, _ = rendered(rig, "a.flac", b"a")
    b, vb = rendered(rig, "b.flac", b"b")
    eng = rig.start_engine()
    rig.client.put("/api/queue", json={"track_ids": [a, b]})
    rig.client.post("/api/player/play")
    eng.wait_for(lambda m: isinstance(m, Play))   # after the load, which sets the position
    eng.position = 5.0
    wait_until(lambda: rig.server.player.position == 5.0, what="the playback position")
    rig.client.put("/api/studio", json={"track_id": b})
    m = eng.mark()
    p = rig.client.put("/api/player/mode", json={"mode": "studio"}).json()
    assert p["mode"] == "studio" and p["slots"] == {"current": {"track_id": b, "version_id": vb}, "next": None}
    sent = eng.since(m, 3)
    assert [type(x).__name__ for x in sent] == ["Pause", "ClearNext", "Load"]
    assert (sent[2].track_id, sent[2].start) == (b, 0.0)
    assert rig.client.post("/api/player/next").status_code == 409   # one track in Studio

    m = eng.mark()
    rig.client.put("/api/player/mode", json={"mode": "playback"})
    sent = eng.since(m, 3)
    assert [type(x).__name__ for x in sent] == ["Pause", "Load", "Load"]
    assert (sent[1].slot, sent[1].track_id, sent[1].start) == ("current", a, 5.0)   # paused where it was
    assert (sent[2].slot, sent[2].track_id) == ("next", b)
    r = rig.client.delete(f"/api/tracks/{a}")
    assert r.status_code == 409   # loaded in the engine


def test_no_removal_while_the_engine_has_the_track_open(rig):
    rig.start_server()
    a, _ = rendered(rig, "a.flac", b"a")
    b, _ = rendered(rig, "b.flac", b"b")
    eng = rig.start_engine()
    rig.client.put("/api/queue", json={"track_ids": [a, b]})
    rig.client.post("/api/player/play")
    old = eng.wait_for(lambda m: isinstance(m, Load) and m.slot == "current").anim_path
    eng.wait_for(lambda m: isinstance(m, Load) and m.slot == "next")
    for t in (a, b):   # playing, next
        r = rig.client.delete(f"/api/tracks/{t}", params={"delete_data": True})
        assert r.status_code == 409 and "loaded" in r.text
    eng.send(TrackEnded(track_id=a))
    eng.send(TrackStarted(track_id=b))
    wait_until(lambda: rig.state()["player"]["current_track_id"] == b, what="track b to start")
    assert rig.client.delete(f"/api/tracks/{a}", params={"delete_data": True}).status_code == 409   # not released
    key = rig.client.get(f"/api/tracks/{a}").json()["key"]
    eng.send(Released(anim_path=old))
    wait_until(lambda: rig.client.delete(f"/api/tracks/{a}", params={"delete_data": True}).status_code == 204,
               what="the removal after the release")
    assert not (rig.data / "tracks" / key).exists()


def test_fps_change_puts_the_playing_track_back_in_the_queue(rig):
    rig.start_server()
    a, va = rendered(rig, "a.flac", b"a")
    b, vb = rendered(rig, "b.flac", b"b")
    eng = rig.start_engine()
    rig.client.put("/api/queue", json={"track_ids": [a, b]})
    assert rig.client.post("/api/player/play").json()["current_track_id"] == a
    assert rig.client.get("/api/settings/fps-impact", params={"fps": 60}).json()["queue_rerenders"] == 2
    rig.client.patch("/api/settings", json={"display_fps": 60})
    eng.wait_for(lambda m: isinstance(m, Stop))
    assert rig.state()["player"]["current_track_id"] is None
    assert [q["track_id"] for q in rig.client.get("/api/queue").json()] == [a, b]
    renders = sorted((j for j in rig.client.get("/api/jobs").json() if j["kind"] == "render"), key=lambda j: j["id"])
    assert [(j["version_id"], j["priority"], j["spec"]["reason"]) for j in renders] == \
        [(va, Priority.QUEUED, "fps"), (vb, Priority.QUEUED, "fps")]
    render_done(rig.server, va, 60, render_id="ra60", anim_path=anim(rig, a, "ra60"))
    assert rig.client.post("/api/player/play").json()["current_track_id"] == a   # plays again once re-rendered


def test_engine_restart_gets_the_track_back_paused(rig):
    rig.start_server()
    a, _ = rendered(rig, "a.flac", b"a")
    eng = rig.start_engine()
    rig.client.put("/api/queue", json={"track_ids": [a]})
    rig.client.post("/api/player/play")
    eng.wait_for(lambda m: isinstance(m, Play))
    eng.position = 7.0
    wait_until(lambda: rig.server.player.position == 7.0)
    rig.stop_engine()
    eng = rig.start_engine("e2")   # a new instance, holding nothing
    load = eng.wait_for(lambda m: isinstance(m, Load))
    assert (load.slot, load.track_id, load.start) == ("current", a, 7.0)
    assert isinstance(eng.got[0], Hello) and isinstance(eng.got[1], Configure) and isinstance(eng.got[2], Pause)
    assert not eng.of(Play)


def test_server_restart_adopts_the_playing_engine(rig):
    rig.start_server()
    a, _ = rendered(rig, "a.flac", b"a")
    b, _ = rendered(rig, "b.flac", b"b")
    eng = rig.start_engine()
    rig.client.put("/api/queue", json={"track_ids": [a, b]})
    rig.client.post("/api/player/play")
    wait_until(lambda: eng.state == "playing")
    rig.stop_server()   # the engine plays on
    m = eng.mark()
    rig.start_server()
    wait_until(lambda: rig.state()["player"]["current_track_id"] == a, what="the adoption")
    p = rig.state()["player"]
    assert p["slots"]["next"]["track_id"] == b
    assert not [x for x in eng.since(m) if not isinstance(x, (Hello, Configure))]   # nothing reloaded
    assert eng.state == "playing"


def test_websocket(rig):
    rig.start_server()
    eng = rig.start_engine()
    with rig.client.websocket_connect("/api/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "state" and first["engine"]["connected"]
        assert first["output"]["nodes"][0]["ip"] == "192.168.1.101"
        frame = encode_preview(9, None, np.full((rig.display.geometry.pixel_count, 3), 7, np.uint8))

        def receive_until(pred):
            for _ in range(500):
                m = ws.receive()
                if pred(m):
                    return m
                if "bytes" not in m or m["bytes"] is None:
                    eng.send(frame)
            raise AssertionError("not received")

        eng.send(frame)
        got = receive_until(lambda m: m.get("bytes") is not None)
        assert got["bytes"] == frame
        receive_until(lambda m: m.get("text") and '"type":"status"' in m["text"])
        upload(rig.client, "a.flac", b"a")
        receive_until(lambda m: m.get("text") and '"what":"tracks"' in m["text"])


# -- cleanup, nodes, children
def test_cleanup_files(tmp_path):
    anim = tmp_path / "tracks" / ("ab" * 8) / "animations"
    anim.mkdir(parents=True)
    for n in ("keep", "open", "gone"):
        (anim / f"{n}.anim").write_bytes(b"x")
    (anim / "new.anim.tmp").write_bytes(b"x")
    (tmp_path / "tracks" / ".import-1.tmp").write_bytes(b"x")
    rel = f"tracks/{'ab' * 8}/animations"
    gone = cleanup.sweep_anims(tmp_path, {f"{rel}/keep.anim", f"{rel}/open.anim"})
    assert [p.name for p in gone] == ["gone.anim"]
    assert sorted(p.name for p in cleanup.leftover_tmp(tmp_path)) == [".import-1.tmp", "new.anim.tmp"]


def test_ping():
    assert ping("127.0.0.1")
    assert not ping("192.0.2.1", timeout_ms=200)   # TEST-NET-1: never answers


def test_node_watch_reports_changes():
    up = {"10.0.0.1"}
    seen = []
    w = NodeWatch([(1, "10.0.0.1"), (2, "10.0.0.2")], seen.append, pinger=lambda ip, src: ip in up)

    async def go():
        await w.check()
        await w.check()          # no change: not reported again
        up.add("10.0.0.2")
        await w.check()
    asyncio.run(go())
    w.close()
    assert seen == [frozenset({1}), frozenset({1, 2})]


def test_armed_follows_nodes_unless_overridden(rig):
    rig.start_server()
    s = rig.server
    assert not s.armed
    s.online = frozenset({1})
    assert s.armed
    s.armed_override = False
    assert not s.armed
    s.armed_override, s.online = True, frozenset()
    assert s.armed


def test_child_restarts_after_exit_and_kill_pid():
    exits = []

    async def go():
        c = Child("t", [sys.executable, "-c", "import sys; sys.exit(3)"], on_exit=exits.append, log=lambda line: None)
        task = asyncio.create_task(c.run())
        c.start()
        while len(exits) < 2:   # it exits, is restarted after 1 s of backoff, exits again
            await asyncio.sleep(0.05)
        await c.kill()
        task.cancel()
    asyncio.run(asyncio.wait_for(go(), 15))
    assert exits[:2] == [3, 3]

    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    assert kill_pid(p.pid)
    assert p.wait(5) is not None
    assert kill_pid(p.pid)   # already gone


def test_spawned_worker_replaces_a_survivor(tmp_path):
    """spawn: a worker still bound to the port is killed, leftover .tmp
    files go, and the server starts and stops its own worker."""
    data = tmp_path / "data"
    (data / "tracks").mkdir(parents=True)
    (data / "tracks" / ".import-x.tmp").write_bytes(b"x")
    rt = Runtime(engine_control_port=free_port(), engine_preview_port=free_port(), worker_port=free_port(),
                 ping=False)
    survivor = subprocess.Popen([sys.executable, "-m", "volu_gen.worker", "--display", str(BENCH), "--data",
                                 str(data), "--port", str(rt.worker_port), "--server-timeout", "0"],
                                stderr=subprocess.DEVNULL)
    try:
        # the PID it reports is the interpreter's (on Windows a venv's python.exe is a launcher)
        old = wait_until(lambda: probe_hello(rt.worker_port), 30, "the survivor worker")
        s = Server(Display.load(BENCH), data, runtime=rt)
        s.log = lambda line: None
        with TestClient(create_app(s), backend_options={"loop_factory": asyncio.SelectorEventLoop}) as c:
            assert survivor.wait(10) is not None
            assert not (data / "tracks" / ".import-x.tmp").exists()
            w = wait_until(lambda: (st := c.get("/api/state").json()["worker"])["connected"] and st, 30, "the worker")
            assert w["pid"] != old
            pid = w["pid"]
        s.close()
        assert kill_pid(pid, 0.1)   # gone already: the server stopped it
    finally:
        survivor.kill()
