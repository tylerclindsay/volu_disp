"""User settings (docs/ui.md → Settings), one DB row per field.

Values that depend on the display (the display FPS must fit its longest
strip) are checked against the active display, passed as validation
context. A stored value the display no longer allows (after a config
change) falls back to its default.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator

from ..geometry import MAX_FPS, MIN_FPS
from . import db as dbm
from .display import Display

AudioMode = Literal["shared", "exclusive"]
DEFAULT_PREVIEW_FPS = 60
MAX_OFFSET_MS = 2000.0


class SyncOffsets(BaseModel):
    """A/V sync offsets for one audio device + output mode
    (docs/architecture.md → Engine → Clock). Positive = lights earlier.

    device         audio device name; None = the system default output
    calibrated_ms  display offset the last calibration set ("reset" returns to it)
    """
    model_config = ConfigDict(extra="forbid")

    device: str | None = None
    mode: AudioMode = "shared"
    display_ms: float = Field(0.0, ge=-MAX_OFFSET_MS, le=MAX_OFFSET_MS)
    preview_ms: float = Field(0.0, ge=-MAX_OFFSET_MS, le=MAX_OFFSET_MS)
    calibrated_ms: float | None = Field(None, ge=-MAX_OFFSET_MS, le=MAX_OFFSET_MS)


class Settings(BaseModel):
    """display_fps        any whole number 30–480 the longest strip fits (decision 0010)
    preview_fps        UI preview rate, ≤ display_fps
    brightness         global, 0–1, applied live by the engine (decision 0020)
    audio_device       None = the system default output
    audio_mode         WASAPI shared or exclusive (decision 0009)
    network_interface  IP of the display network interface to send from; None = any
    sync               offsets per audio device + mode
    """
    model_config = ConfigDict(extra="forbid")

    display_fps: int = Field(ge=MIN_FPS, le=MAX_FPS)
    preview_fps: int = Field(ge=1, le=MAX_FPS)
    brightness: float = Field(1.0, ge=0.0, le=1.0)
    audio_device: str | None = None
    audio_mode: AudioMode = "shared"
    network_interface: str | None = None
    sync: list[SyncOffsets] = []

    @field_validator("display_fps")
    @classmethod
    def _fits_display(cls, v: int, info: ValidationInfo) -> int:
        display: Display | None = (info.context or {}).get("display")
        if display is not None and (problems := display.fps_problems(v)):
            raise ValueError("; ".join(problems))
        return v

    @field_validator("preview_fps")
    @classmethod
    def _preview_at_most_display(cls, v: int, info: ValidationInfo) -> int:
        fps = info.data.get("display_fps")
        if fps is not None and v > fps:
            raise ValueError(f"preview FPS {v} is above the display FPS {fps}")
        return v

    @field_validator("sync")
    @classmethod
    def _one_entry_per_output(cls, v: list[SyncOffsets]) -> list[SyncOffsets]:
        keys = [(s.device, s.mode) for s in v]
        if len(set(keys)) != len(keys):
            raise ValueError("more than one sync entry for the same audio device and mode")
        return v

    def offsets(self) -> SyncOffsets:
        """The offsets of the current audio device + mode (zeros if never set)."""
        for s in self.sync:
            if (s.device, s.mode) == (self.audio_device, self.audio_mode):
                return s
        return SyncOffsets(device=self.audio_device, mode=self.audio_mode)

    def with_offsets(self, offsets: SyncOffsets) -> Settings:
        """A copy with `offsets` stored for its device + mode."""
        rest = [s for s in self.sync if (s.device, s.mode) != (offsets.device, offsets.mode)]
        return self.model_copy(update={"sync": [*rest, offsets]})


def defaults(display: Display) -> dict:
    fps = min(display.config.default_fps, display.max_fps)
    return {"display_fps": fps, "preview_fps": min(DEFAULT_PREVIEW_FPS, fps)}


def validate(data: dict, display: Display) -> Settings:
    return Settings.model_validate(data, context={"display": display})


def load(db: sqlite3.Connection, display: Display) -> Settings:
    """Stored settings over the defaults. Stored fields the model or the
    display no longer accepts are dropped (back to their defaults)."""
    stored = {r["key"]: dbm.loads(r["value"]) for r in db.execute("SELECT key, value FROM settings")}
    stored = {k: v for k, v in stored.items() if k in Settings.model_fields}
    data = {**defaults(display), **stored}
    try:
        return validate(data, display)
    except ValidationError as e:
        bad = {err["loc"][0] for err in e.errors() if err["loc"]}
        if "display_fps" in bad:
            bad.add("preview_fps")   # it may only have fit the old rate
        return validate({**defaults(display), **{k: v for k, v in stored.items() if k not in bad}},
                        display)


def save(db: sqlite3.Connection, settings: Settings):
    with db:
        db.executemany("INSERT INTO settings (key, value) VALUES (?, ?) "
                       "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                       [(k, json.dumps(v)) for k, v in settings.model_dump(mode="json").items()])


def patch(current: Settings, changes: dict, display: Display) -> Settings:
    """Apply a partial update. Lowering the display FPS below the preview
    FPS lowers the preview FPS too, unless the change also sets it."""
    data = {**current.model_dump(mode="json"), **changes}
    fps = data.get("display_fps")
    if "preview_fps" not in changes and isinstance(fps, int) and data["preview_fps"] > fps:
        data["preview_fps"] = fps
    return validate(data, display)
