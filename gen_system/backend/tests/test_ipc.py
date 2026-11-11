import socket
import time

import numpy as np
import pytest
import zmq

from volu_gen.ipc import (FROM_ENGINE, FROM_WORKER, TO_ENGINE, TO_WORKER, AnalyzeResult, AnalyzeSpec, Cancel,
                          Cancelled, ClearNext, Configure, Done, EngineError, EngineHello, Failed, Hello, Load,
                          Log, MessageError, Pause, Play, Progress, RegridResult, RegridSpec, Released,
                          RenderResult, RenderSpec, Seek, Shutdown, SlotState, Status, Stop, Submit, SwapAnim,
                          Test, TestOff, TrackEnded, TrackStarted, WorkerHello, bind_pub, bind_router,
                          connect_dealer, connect_sub, decode, decode_preview, encode, encode_preview, send_to)
from volu_gen.ipc.sockets import PortInUse

KEY = "0f" * 8   # a track key: 16 hex digits
RENDER = RenderSpec(key=KEY, render_id="r1", anim_path=f"tracks/{KEY}/animations/r1.anim", generator="director",
                    plan={"segments": []}, fps=97, geometry_hash="h")
SUMMARY = {"key": KEY, "duration": 20.0, "bpm": 120.0, "tempo_mode": "grid", "grid_override": None}

TO_ENGINE_MSGS = [
    Hello(),
    Configure(display_config="c.yaml", geometry_hash="h", display_fps=120, preview_fps=60, online_nodes=[0, 1],
              display_offset_ms=-12.5, brightness=0.5, audio_mode="exclusive"),
    Load(slot="next", track_id=3, audio_path=f"tracks/{KEY}/source.flac", anim_path=RENDER.anim_path, start=1.5),
    SwapAnim(anim_path=RENDER.anim_path), Play(), Pause(), Stop(), Seek(t=12.25), ClearNext(),
    Test(pattern="sweep", params={"axis": "x"}), TestOff(), Shutdown(),
]
FROM_ENGINE_MSGS = [
    EngineHello(instance_id="e1", pid=42, state="playing", position=3.0,
                slots=[SlotState(slot="current", track_id=3, audio_path="a", anim_path="b")]),
    Status(state="paused", track_id=3, position=1.0, frame=120, fps=119.9, late_frames=2, xruns=1),
    TrackStarted(track_id=4), TrackEnded(track_id=3), Released(anim_path="b"),
    EngineError(kind="audio", message="device lost"),
]
TO_WORKER_MSGS = [
    Hello(), Submit(job_id=1, spec=AnalyzeSpec(key=KEY)),
    Submit(job_id=2, spec=AnalyzeSpec(key=KEY, stages=["peaks"], force=["peaks"])),
    Submit(job_id=3, spec=RegridSpec(key=KEY, bpm=128, offset=0.25)),
    Submit(job_id=4, spec=RegridSpec(key=KEY)), Submit(job_id=5, spec=RENDER), Cancel(job_id=5),
]
FROM_WORKER_MSGS = [
    WorkerHello(pid=7), WorkerHello(pid=7, job_id=5),
    Progress(job_id=5, stage="render", track="a", frac=0.5, eta=3.0),
    Done(job_id=1, result=AnalyzeResult(**SUMMARY, stages={"stems": {"seconds": 1.0}})),
    Done(job_id=3, result=RegridResult(**{**SUMMARY, "grid_override": {"bpm": 128.0, "offset": 0.25}})),
    Done(job_id=5, result=RenderResult(render_id="r1", anim_path=RENDER.anim_path, frame_count=10, fps=97,
                                       pixel_count=4, geometry_hash="h", format_version=1, generator="director",
                                       generator_version=1, grid_override=None)),
    Failed(job_id=6, error="ValueError: x", traceback="Traceback ..."), Cancelled(job_id=5), Log(line="[stems] 1"),
]


@pytest.mark.parametrize("adapter, msg", [(TO_ENGINE, m) for m in TO_ENGINE_MSGS]
                         + [(FROM_ENGINE, m) for m in FROM_ENGINE_MSGS]
                         + [(TO_WORKER, m) for m in TO_WORKER_MSGS]
                         + [(FROM_WORKER, m) for m in FROM_WORKER_MSGS], ids=lambda x: getattr(x, "type", ""))
def test_roundtrip(adapter, msg):
    back = decode(adapter, encode(msg))
    assert type(back) is type(msg) and back == msg


def test_submit_spec_from_a_dict():
    s = decode(TO_WORKER, encode(Submit(job_id=1, spec={"kind": "regrid", "key": KEY, "bpm": 120, "offset": 0})))
    assert s.kind == "regrid" and isinstance(s.spec, RegridSpec)


@pytest.mark.parametrize("data", [
    b"\xc1",                                                   # not msgpack
    encode(Status(state="playing")),                           # an engine message sent to the worker
    encode(Hello())[:-1] + b"\x00",                            # truncated / changed
])
def test_decode_rejects(data):
    with pytest.raises(MessageError):
        decode(TO_WORKER, data)


def test_spec_checks():
    with pytest.raises(ValueError, match="both"):
        RegridSpec(key=KEY, bpm=120)
    with pytest.raises(ValueError):
        AnalyzeSpec(key="../../etc")                           # keys are 16 hex digits
    with pytest.raises(ValueError):
        RenderSpec(**{**RENDER.model_dump(), "fps": 500})
    with pytest.raises(ValueError):
        RenderSpec(**{**RENDER.model_dump(), "render_id": "../x"})
    with pytest.raises(ValueError):
        Progress(job_id=1, stage="x", frac=1.5)


def test_preview():
    rgb = np.arange(30, dtype=np.uint8).reshape(10, 3)
    p = decode_preview(encode_preview(1234, 7, rgb))
    assert (p.frame, p.track_id) == (1234, 7) and np.array_equal(p.rgb, rgb)
    p = decode_preview(encode_preview(0, None, rgb[:0]))
    assert p.track_id is None and p.rgb.shape == (0, 3)
    with pytest.raises(MessageError):
        decode_preview(encode_preview(0, 1, rgb)[:-1])
    with pytest.raises(ValueError):
        encode_preview(0, 1, rgb.astype(np.float32))


# -- sockets
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def ctx():
    c = zmq.Context()
    yield c
    c.term()


def test_router_dealer(ctx):
    port = free_port()
    router = bind_router(ctx, port)
    dealer = connect_dealer(ctx, port)
    try:
        dealer.send(encode(Hello()))
        assert router.poll(2000)
        peer, data = router.recv_multipart()
        assert peer == b"server" and decode(TO_WORKER, data) == Hello()
        assert send_to(router, peer, WorkerHello(pid=1))
        assert dealer.poll(2000) and decode(FROM_WORKER, dealer.recv()) == WorkerHello(pid=1)
        assert not send_to(router, b"nobody", Log(line="x"))   # absent peer: dropped, no exception
    finally:
        dealer.close()
        router.close()


def test_dealer_queues_nothing_without_a_peer(ctx):
    dealer = connect_dealer(ctx, free_port())
    try:
        with pytest.raises(zmq.Again):
            dealer.send(encode(Hello()), zmq.NOBLOCK)
    finally:
        dealer.close()


def test_port_in_use(ctx):
    port = free_port()
    router = bind_router(ctx, port)
    try:
        with pytest.raises(PortInUse):
            bind_router(ctx, port)
    finally:
        router.close()


def test_preview_is_latest_only(ctx):
    port = free_port()
    pub, sub = bind_pub(ctx, port), connect_sub(ctx, port)
    try:
        rgb = np.zeros((4, 3), np.uint8)
        deadline = time.monotonic() + 5
        while not sub.poll(10):                                # wait out the slow-joiner window
            pub.send(encode_preview(-1, None, rgb))
            assert time.monotonic() < deadline
        sub.recv()
        for i in range(5):
            pub.send(encode_preview(i, 1, rgb))
        time.sleep(0.2)
        assert decode_preview(sub.recv()).frame == 4
        assert not sub.poll(100)
    finally:
        sub.close()
        pub.close()
