"""Every message between the API server, the engine and the worker
(docs/architecture.md → IPC, decision 0007).

Control messages are pydantic models with a `type` field, dumped to dicts
and encoded with MessagePack (`encode` / `decode`; no pickle). Each
direction has its own union (`ToEngine`, `FromEngine`, `ToWorker`,
`FromWorker`), so the engine's `hello` and the worker's `hello` are
different models with the same `type`.

Preview frames are not pydantic models: a fixed little-endian header
(frame index, track id) and then the raw RGB bytes in geometry order
(`encode_preview` / `decode_preview`). Each is one ZeroMQ frame, because
CONFLATE does not support multipart messages.

Paths in messages are relative to the data dir (`RelPath` rejects absolute
paths and ".."). The server gives each child the data dir when it starts it.

Kept light: the engine imports this module, so no generators or analysis
imports here. The worker validates a render's plan itself.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Annotated, Any, Literal, TypeVar

import msgpack
import numpy as np
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from ..geometry import MAX_FPS, MIN_FPS


def _relative(path: str) -> str:
    # PureWindowsPath reads both separators and catches "C:x" and "\x" as
    # well as "/x"; any of them would replace the data dir in `data_dir / path`.
    p = PureWindowsPath(path)
    if not path or p.drive or p.root or ".." in p.parts:
        raise ValueError(f"{path!r} is not a path relative to the data dir")
    return path


AudioMode = Literal["shared", "exclusive"]   # WASAPI output mode (decision 0009)
Slot = Literal["current", "next"]
Key = Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")]   # track dir name: sha256[:16] of the source
RelPath = Annotated[str, AfterValidator(_relative)]     # relative to the data dir, no ".."


T = TypeVar("T")


class MessageError(ValueError):
    """Bytes that are not a valid message for the receiving side."""


class Msg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# -- shared
class Hello(Msg):
    """Server → engine or worker, on every (re)connect. The child replies
    with its own `hello`."""
    type: Literal["hello"] = "hello"


# -- server → engine
class Configure(Msg):
    """The engine's full configuration. Sent after every engine `hello` and
    on any change; the engine keeps no state that this does not restore.

    display_config     path of the display config (as given to the server)
    geometry_hash      the server's; the engine refuses files of another geometry
    audio_device       None = the system default output
    display_offset_ms  A/V sync offset, positive = lights earlier
    network_interface  IP of the display network interface to send from; None = any
    armed              send frames to the nodes (else preview only)
    online_nodes       drive node ids that answered the server's ping
    brightness         global, 0–1, applied to sent and preview frames (decision 0020)
    """
    type: Literal["configure"] = "configure"
    display_config: str
    geometry_hash: str
    audio_device: str | None = None
    audio_mode: AudioMode = "shared"
    display_fps: int = Field(ge=MIN_FPS, le=MAX_FPS)
    preview_fps: int = Field(ge=1, le=MAX_FPS)
    display_offset_ms: float = 0.0
    preview_offset_ms: float = 0.0
    network_interface: str | None = None
    armed: bool = False
    online_nodes: list[int] = []
    brightness: float = Field(1.0, ge=0.0, le=1.0)


class Load(Msg):
    """Load a track into a slot: its audio and its animation, from `start` s."""
    type: Literal["load"] = "load"
    slot: Slot
    track_id: int
    audio_path: RelPath
    anim_path: RelPath
    start: float = Field(0.0, ge=0.0)


class SwapAnim(Msg):
    """Replace the current slot's animation at the playhead; audio and
    playhead stay the same."""
    type: Literal["swap_anim"] = "swap_anim"
    anim_path: RelPath


class Play(Msg):
    type: Literal["play"] = "play"


class Pause(Msg):
    type: Literal["pause"] = "pause"


class Stop(Msg):
    type: Literal["stop"] = "stop"


class Seek(Msg):
    type: Literal["seek"] = "seek"
    t: float = Field(ge=0.0)


class ClearNext(Msg):
    """Drop the preloaded next track."""
    type: Literal["clear_next"] = "clear_next"


class Test(Msg):
    """Show a test pattern or the calibration flash + click."""
    __test__ = False   # not a pytest class
    type: Literal["test"] = "test"
    pattern: str
    params: dict[str, Any] = {}


class TestOff(Msg):
    __test__ = False
    type: Literal["test_off"] = "test_off"


class Shutdown(Msg):
    type: Literal["shutdown"] = "shutdown"


# -- engine → server
PlayState = Literal["stopped", "paused", "playing"]


class SlotState(Msg):
    slot: Slot
    track_id: int
    audio_path: RelPath
    anim_path: RelPath


class EngineHello(Msg):
    """Reply to the server's `hello`: who the engine is and what it holds,
    so a restarted server can adopt it."""
    type: Literal["hello"] = "hello"
    instance_id: str
    pid: int
    state: PlayState
    slots: list[SlotState] = []
    position: float | None = None


class Status(Msg):
    """~20 Hz; also the engine's heartbeat.

    position     s into the current track; None with no track
    fps          measured frame rate
    late_frames  frames repeated because their chunk was not decoded yet
    xruns        audio buffer underruns
    """
    type: Literal["status"] = "status"
    state: PlayState
    track_id: int | None = None
    position: float | None = None
    frame: int | None = None
    fps: float = 0.0
    late_frames: int = 0
    xruns: int = 0


class TrackStarted(Msg):
    type: Literal["track_started"] = "track_started"
    track_id: int


class TrackEnded(Msg):
    type: Literal["track_ended"] = "track_ended"
    track_id: int


class Released(Msg):
    """The engine closed this animation file: the server may delete it."""
    type: Literal["released"] = "released"
    anim_path: RelPath


class EngineError(Msg):
    type: Literal["error"] = "error"
    kind: str
    message: str


# -- worker jobs: specs and results (docs/architecture.md → Worker)
class AnalyzeSpec(Msg):
    """Analyze one track. stages None = all; finished stages are skipped
    unless in `force`. The worker checks the stage names."""
    kind: Literal["analyze"] = "analyze"
    key: Key
    stages: list[str] | None = None
    force: list[str] = []


class RegridSpec(Msg):
    """Set the user grid (BPM + time of any downbeat, s), or clear it with
    both None."""
    kind: Literal["regrid"] = "regrid"
    key: Key
    bpm: float | None = Field(None, gt=0)
    offset: float | None = None

    @model_validator(mode="after")
    def _both_or_neither(self):
        if (self.bpm is None) != (self.offset is None):
            raise ValueError("give both bpm and offset, or neither to clear the override")
        return self


class RenderSpec(Msg):
    """Render a plan into `anim_path` (tracks/<key>/animations/<render_id>.anim).

    plan           the Plan JSON made by the server; it carries the track params
    fps            the display FPS when the job started (decision 0010)
    geometry_hash  the server's; the worker refuses a render for another geometry
    """
    kind: Literal["render"] = "render"
    key: Key
    render_id: str = Field(pattern=r"^[0-9A-Za-z_-]+$")
    anim_path: RelPath
    generator: str
    plan: dict[str, Any]
    fps: int = Field(ge=MIN_FPS, le=MAX_FPS)
    geometry_hash: str


JobSpec = Annotated[AnalyzeSpec | RegridSpec | RenderSpec, Field(discriminator="kind")]


class GridSummary(Msg):
    """What the Library shows of a track's analysis (the DB's copy;
    analysis.json stays the source of truth)."""
    key: Key
    duration: float | None
    bpm: float | None
    tempo_mode: str | None
    grid_override: dict[str, float] | None


class AnalyzeResult(GridSummary):
    """stages: per stage that ran, seconds and peak VRAM (GB)."""
    kind: Literal["analyze"] = "analyze"
    stages: dict[str, dict[str, Any]] = {}


class RegridResult(GridSummary):
    """The new phrases, events and bars are in analysis.json."""
    kind: Literal["regrid"] = "regrid"


class RenderResult(Msg):
    """The rendered file and what the version row records about it."""
    kind: Literal["render"] = "render"
    render_id: str
    anim_path: RelPath
    frame_count: int
    fps: int
    pixel_count: int
    geometry_hash: str
    format_version: int
    generator: str
    generator_version: int
    grid_override: dict[str, float] | None   # the tempo override the render used


JobResult = Annotated[AnalyzeResult | RegridResult | RenderResult, Field(discriminator="kind")]


# -- server → worker
class Submit(Msg):
    """Run a job. The worker runs one job at a time."""
    type: Literal["submit"] = "submit"
    job_id: int
    spec: JobSpec

    @property
    def kind(self) -> str:
        return self.spec.kind


class Cancel(Msg):
    """Cancel a running render between chunks. Other jobs cancel only by
    killing the worker."""
    type: Literal["cancel"] = "cancel"
    job_id: int


# -- worker → server
class WorkerHello(Msg):
    """Reply to the server's `hello`. job_id: the job the worker runs, or
    None when idle."""
    type: Literal["hello"] = "hello"
    pid: int
    job_id: int | None = None


class Progress(Msg):
    """frac 0–1; eta in s when known."""
    type: Literal["progress"] = "progress"
    job_id: int
    stage: str
    track: str | None = None
    frac: float = Field(ge=0.0, le=1.0)
    eta: float | None = None


class Done(Msg):
    type: Literal["done"] = "done"
    job_id: int
    result: JobResult


class Failed(Msg):
    type: Literal["failed"] = "failed"
    job_id: int
    error: str
    traceback: str = ""


class Cancelled(Msg):
    """A render stopped on `cancel`; no file was written."""
    type: Literal["cancelled"] = "cancelled"
    job_id: int


class Log(Msg):
    type: Literal["log"] = "log"
    line: str


ToEngine = Annotated[Hello | Configure | Load | SwapAnim | Play | Pause | Stop | Seek | ClearNext | Test
                     | TestOff | Shutdown, Field(discriminator="type")]
FromEngine = Annotated[EngineHello | Status | TrackStarted | TrackEnded | Released | EngineError,
                       Field(discriminator="type")]
ToWorker = Annotated[Hello | Submit | Cancel, Field(discriminator="type")]
FromWorker = Annotated[WorkerHello | Progress | Done | Failed | Cancelled | Log, Field(discriminator="type")]

TO_ENGINE: TypeAdapter[ToEngine] = TypeAdapter(ToEngine)
FROM_ENGINE: TypeAdapter[FromEngine] = TypeAdapter(FromEngine)
TO_WORKER: TypeAdapter[ToWorker] = TypeAdapter(ToWorker)
FROM_WORKER: TypeAdapter[FromWorker] = TypeAdapter(FromWorker)


def encode(msg: Msg) -> bytes:
    return msgpack.packb(msg.model_dump(mode="json"), use_bin_type=True)


def decode(adapter: TypeAdapter[T], data: bytes) -> T:
    """One message for the receiving side's union (TO_ENGINE, …). Raises
    MessageError for bad bytes or a message that does not validate."""
    try:
        return adapter.validate_python(msgpack.unpackb(data, raw=False))
    except (ValueError, TypeError) as e:   # msgpack errors and ValidationError are ValueErrors
        raise MessageError(str(e)) from e


# -- preview frames (engine → server, latest-only)
PREVIEW_HEADER = struct.Struct("<qq")   # frame index, track id (-1: none, e.g. a test pattern)


@dataclass(frozen=True, eq=False)
class Preview:
    """rgb: (P, 3) uint8 in geometry order (a read-only view of the message)."""
    frame: int
    track_id: int | None
    rgb: np.ndarray


def encode_preview(frame: int, track_id: int | None, rgb: np.ndarray) -> bytes:
    rgb = np.asarray(rgb)
    if rgb.dtype != np.uint8 or rgb.ndim != 2 or rgb.shape[1] != 3:
        raise ValueError(f"expected uint8 (P, 3), got {rgb.dtype} {rgb.shape}")
    return PREVIEW_HEADER.pack(frame, -1 if track_id is None else track_id) + rgb.tobytes()


def decode_preview(data: bytes) -> Preview:
    n = len(data) - PREVIEW_HEADER.size
    if n < 0 or n % 3:
        raise MessageError(f"preview of {len(data)} bytes: not a header plus whole RGB pixels")
    frame, track_id = PREVIEW_HEADER.unpack_from(data)
    rgb = np.frombuffer(data, np.uint8, offset=PREVIEW_HEADER.size).reshape(-1, 3)
    return Preview(frame, None if track_id < 0 else track_id, rgb)
