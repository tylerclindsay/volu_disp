import copy
import threading
import time

import pytest
import zmq
from test_ipc import free_port
from test_methods import GEO
from test_methods import analysis as method_analysis
from test_methods import features as method_features

from volu_gen.analysis import pipeline
from volu_gen.analysis.audio import STEM_NAMES
from volu_gen.analysis.features import write_features
from volu_gen.analysis.grid import grid_times
from volu_gen.analysis.track import TrackDir, import_track
from volu_gen.animfile import FORMAT_VERSION, AnimReader
from volu_gen.animfile import tmp_path as anim_tmp_path
from volu_gen.generators import (DIRECTOR_VERSION, GENERATOR_ID, DirectorParams, Music, RenderCancelled, StalePlan,
                                 make_plan)
from volu_gen.ipc import (FROM_WORKER, AnalyzeSpec, Cancel, Cancelled, Done, Failed, Hello, Log, Progress,
                          RegridSpec, RenderSpec, Submit, WorkerHello, connect_dealer, decode, encode)
from volu_gen.worker import JobEnv, JobError, Worker, run_analyze, run_regrid, run_render
from volu_gen.worker.worker import PROGRESS_INTERVAL

FPS = 30


@pytest.fixture
def data(tmp_path):
    """A data dir with one imported track: synthetic analysis (with the
    fitted grid a regrid needs) and features; no stems."""
    data = tmp_path / "data"
    src = tmp_path / "a.flac"
    src.write_bytes(b"audio")
    t = import_track(src, data)
    a = method_analysis()
    beats, downbeats = grid_times(a["tempo"]["bpm"], a["tempo"]["offset"], a["source"]["duration"])
    a["tempo"]["fit"] = {"used": True, "bpm": a["tempo"]["bpm"], "offset": a["tempo"]["offset"]}
    a["tempo"]["raw"] = {"beats": beats.tolist(), "downbeats": downbeats.tolist()}
    a["source"] = {**t.data["source"], "duration": a["source"]["duration"]}
    t.data.update(a)
    t.save()
    write_features(t.features_path, method_features())
    return data


@pytest.fixture
def key(data) -> str:
    return next((data / "tracks").iterdir()).name


def render_spec(key: str, render_id="r1", **over) -> RenderSpec:
    plan = make_plan(Music(method_analysis(), method_features()), DirectorParams(seed=4))
    return RenderSpec(**{"key": key, "render_id": render_id, "anim_path": f"tracks/{key}/animations/{render_id}.anim",
                         "generator": GENERATOR_ID, "plan": plan.model_dump(mode="json"), "fps": FPS,
                         "geometry_hash": GEO.hash, **over})


# -- jobs
def test_render(data, key):
    seen = []
    env = JobEnv(data, GEO, progress=lambda *a: seen.append(a))
    r = run_render(render_spec(key), env)
    path = data / r.anim_path
    with AnimReader(path) as rd:
        assert len(rd) == r.frame_count == 600
        assert rd.meta["render_id"] == "r1" and rd.meta["fps"] == FPS
    assert (r.fps, r.pixel_count, r.geometry_hash) == (FPS, GEO.pixel_count, GEO.hash)
    assert (r.format_version, r.generator, r.generator_version) == (FORMAT_VERSION, GENERATOR_ID, DIRECTOR_VERSION)
    assert r.grid_override is None
    assert seen[0][2] == 0.0 and seen[-1][2] == 1.0 and seen[-1][3] == 0.0
    assert not anim_tmp_path(path).exists()


@pytest.mark.parametrize("over, match", [
    ({"geometry_hash": "other"}, "geometry"),
    ({"generator": "nope"}, "generator"),
    ({"anim_path": "tracks/elsewhere/r1.anim"}, "anim_path"),
    ({"key": "ffffffffffffffff", "anim_path": "tracks/ffffffffffffffff/animations/r1.anim"}, "no imported track"),
])
def test_render_refuses(data, key, over, match):
    with pytest.raises(JobError, match=match):
        run_render(render_spec(**{"key": key, **over}), JobEnv(data, GEO))
    assert not list(data.rglob("*.anim*"))


@pytest.mark.parametrize("stale", ["director", "method"])
def test_render_refuses_a_stale_plan(data, key, stale):
    """The worker renders exactly the plan it gets; re-planning is the
    server's (decision 0018)."""
    spec = render_spec(key)
    plan = copy.deepcopy(spec.plan)
    if stale == "director":
        plan["director"]["version"] = DIRECTOR_VERSION + 1
    else:
        plan["segments"][0]["method_version"] += 1
    with pytest.raises(StalePlan):
        run_render(spec.model_copy(update={"plan": plan}), JobEnv(data, GEO))
    assert not list(data.rglob("*.anim*"))


def test_render_cancel_leaves_no_file(data, key):
    calls = []

    def cancelled():
        calls.append(1)
        return len(calls) >= 3

    with pytest.raises(RenderCancelled):
        run_render(render_spec(key), JobEnv(data, GEO, cancelled=cancelled))
    assert not list(data.rglob("*.anim*"))


def test_regrid(data, key):
    r = run_regrid(RegridSpec(key=key, bpm=125.0, offset=0.25), JobEnv(data, GEO))
    assert r.grid_override == {"bpm": 125.0, "offset": 0.25} and r.bpm == 125.0 and r.tempo_mode == "grid"
    r = run_regrid(RegridSpec(key=key), JobEnv(data, GEO))
    assert r.grid_override is None and r.bpm == 120.0


def test_analyze_checks_stages(data, key):
    with pytest.raises(JobError, match="unknown stages"):
        run_analyze(AnalyzeSpec(key=key, stages=["stems", "colour"]), JobEnv(data, GEO))
    with pytest.raises(JobError, match="unknown stages"):
        run_analyze(AnalyzeSpec(key=key, force=["nope"]), JobEnv(data, GEO))


def test_analyze_stops_at_the_first_failed_stage(data, key, monkeypatch):
    t = TrackDir(data / "tracks" / key)
    for name in STEM_NAMES:   # stand-ins: the stages that read them are replaced below
        t.stem_path(name).parent.mkdir(exist_ok=True)
        t.stem_path(name).write_bytes(b"")
    ran = []

    def peaks(track):
        raise RuntimeError("peaks broke")

    monkeypatch.setattr(pipeline, "run_peaks", peaks)
    monkeypatch.setattr(pipeline, "run_features", lambda track: ran.append("features"))
    seen = []
    env = JobEnv(data, GEO, device="cpu", log=lambda line: None, progress=lambda *a: seen.append(a[:3]))
    with pytest.raises(RuntimeError, match="peaks broke"):
        run_analyze(AnalyzeSpec(key=key, stages=["peaks", "features"]), env)
    assert ran == [] and seen == [("peaks", t.name, 0.0)]


# -- the process
class Client:
    """The server's end: a DEALER, as the server will hold one."""

    def __init__(self, ctx: zmq.Context, port: int):
        self.sock = connect_dealer(ctx, port)

    def send(self, msg):
        self.sock.send(encode(msg))

    def recv(self, timeout=5.0):
        assert self.sock.poll(int(timeout * 1000)), "no message from the worker"
        return decode(FROM_WORKER, self.sock.recv())

    def until(self, cls, timeout=10.0):
        """Messages up to and including the first of type `cls`."""
        out = []
        deadline = time.monotonic() + timeout
        while not out or not isinstance(out[-1], cls):
            out.append(self.recv(max(deadline - time.monotonic(), 0.01)))
        return out

    def close(self):
        self.sock.close()


class Gate:
    """A fake job that runs until released (or cancelled)."""

    def __init__(self):
        self.go = threading.Event()

    def __call__(self, spec, env: JobEnv):
        env.log("gate waiting")
        while not self.go.wait(0.01):
            if env.cancelled():
                raise RenderCancelled(spec.render_id)
        return run_regrid(spec, env) if isinstance(spec, RegridSpec) else None


@pytest.fixture
def ctx():
    c = zmq.Context()
    yield c
    c.term()


@pytest.fixture
def serve(data):
    """Start a Worker in a thread; returns (worker, port)."""
    started = []

    def start(**kw):
        port = free_port()
        w = Worker(data, GEO, port, **{"server_timeout": 0, "log": lambda line: None, **kw})
        t = threading.Thread(target=w.run, daemon=True)
        t.start()
        started.append((w, t))
        return w, port

    yield start
    for w, t in started:
        w.stop()
        t.join(5)
        w.close()


def test_hello_and_render(serve, ctx, data, key):
    w, port = serve()
    c = Client(ctx, port)
    try:
        c.send(Hello())
        hello = c.recv()
        assert isinstance(hello, WorkerHello) and hello.job_id is None and hello.pid > 0
        c.send(Submit(job_id=7, spec=render_spec(key)))
        msgs = c.until(Done, timeout=60)
        done = msgs[-1]
        assert done.job_id == 7 and done.result.frame_count == 600
        assert (data / done.result.anim_path).exists()
        # a render this small can finish inside one progress interval, and its result drops the waiting progress
        assert all(m.job_id == 7 and m.stage == "render" for m in msgs if isinstance(m, Progress))
        c.send(Hello())
        assert c.recv().job_id is None
    finally:
        c.close()


def test_failure_carries_the_error(serve, ctx, key):
    w, port = serve()
    c = Client(ctx, port)
    try:
        c.send(Hello())
        c.recv()
        c.send(Submit(job_id=1, spec=render_spec(key, geometry_hash="other")))
        f = c.until(Failed)[-1]
        assert f.job_id == 1 and f.error.startswith("JobError: render is for geometry other")
        assert "Traceback" in f.traceback
    finally:
        c.close()


def test_busy_cancel_and_bad_bytes(serve, ctx, key):
    gate = Gate()
    w, port = serve(runners={"render": gate, "regrid": gate})
    c = Client(ctx, port)
    try:
        c.send(Hello())
        c.recv()
        c.sock.send(b"\xc1 not msgpack")                       # dropped; the worker carries on
        c.send(Submit(job_id=1, spec=render_spec(key)))
        assert c.recv() == Log(line="gate waiting")
        c.send(Submit(job_id=1, spec=render_spec(key)))       # the same job again: ignored
        c.send(Submit(job_id=2, spec=RegridSpec(key=key)))
        assert c.recv() == Failed(job_id=2, error="worker busy with job 1")
        c.send(Hello())
        assert c.recv().job_id == 1
        c.send(Cancel(job_id=1))
        assert c.recv() == Cancelled(job_id=1)
        c.send(Hello())
        assert c.recv().job_id is None
    finally:
        gate.go.set()
        c.close()


def test_regrid_cannot_be_cancelled(serve, ctx, key):
    gate = Gate()
    w, port = serve(runners={"regrid": gate})
    c = Client(ctx, port)
    try:
        c.send(Hello())
        c.recv()
        c.send(Submit(job_id=3, spec=RegridSpec(key=key, bpm=126.0, offset=0.5)))
        c.recv()                                               # its log line
        c.send(Cancel(job_id=3))
        assert not c.sock.poll(300)                            # cancel ignored: still running
        gate.go.set()
        done = c.until(Done)[-1]
        assert done.job_id == 3 and done.result.grid_override == {"bpm": 126.0, "offset": 0.5}
    finally:
        gate.go.set()
        c.close()


def test_result_survives_a_reconnect(serve, ctx, key):
    """A result finished while the server is away arrives before the reply
    to its next hello, and the job counts as running until then."""
    gate = Gate()
    w, port = serve(runners={"regrid": gate})
    c = Client(ctx, port)
    c.send(Hello())
    c.recv()
    c.send(Submit(job_id=4, spec=RegridSpec(key=key)))
    c.recv()
    c.close()
    deadline = time.monotonic() + 5
    while w.watch.peers:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    gate.go.set()
    time.sleep(0.3)                                            # the result is ready, with no server to take it
    assert w.busy
    c = Client(ctx, port)
    try:
        c.send(Hello())
        done, hello = c.recv(), c.recv()
        assert isinstance(done, Done) and done.job_id == 4
        assert hello.job_id is None and not w.busy
    finally:
        c.close()


def test_exits_without_a_server(serve, ctx):
    w, port = serve(server_timeout=0.3)
    c = Client(ctx, port)
    c.send(Hello())
    c.recv()
    t0 = time.monotonic()
    c.close()
    thread = next(t for t in threading.enumerate() if getattr(t, "_target", None) == w.run)
    thread.join(5)
    assert not thread.is_alive() and time.monotonic() - t0 >= 0.3


def test_progress_is_latest_only(data, key):
    """At most one progress per PROGRESS_INTERVAL, the newest; a result
    drops its job's waiting progress."""
    w = Worker(data, GEO, free_port(), server_timeout=0, log=lambda line: None)

    def prog(frac):
        return Progress(job_id=1, stage="render", frac=frac)

    try:
        sent = []
        w._server = b"server"
        w._send = lambda m: sent.append(m) or True
        for i in range(5):
            w._outbox.put(prog(i / 4))
        w._outbox.put(Log(line="x"))
        w._outbox.put(prog(1.0))
        w._flush()
        assert sent == [Log(line="x"), prog(1.0)]
        w._outbox.put(prog(0.1))
        w._flush()
        w._outbox.put(prog(0.2))
        w._flush()
        assert sent == [Log(line="x"), prog(1.0)]               # too soon: waits
        time.sleep(2 * PROGRESS_INTERVAL)   # monotonic() ticks ~16 ms on Windows
        w._flush()
        assert sent[2:] == [prog(0.2)]                          # the newest of the two
        time.sleep(2 * PROGRESS_INTERVAL)   # monotonic() ticks ~16 ms on Windows
        w._outbox.put(prog(0.3))
        w._outbox.put(Cancelled(job_id=1))
        w._flush()
        assert sent[3:] == [Cancelled(job_id=1)]
    finally:
        w.close()


def test_cli_rejects_missing_config(tmp_path):
    from volu_gen.worker.__main__ import main
    with pytest.raises(SystemExit) as e:
        main(["--display", str(tmp_path / "nope.yaml"), "--data", str(tmp_path)])
    assert str(e.value).startswith("worker:")
