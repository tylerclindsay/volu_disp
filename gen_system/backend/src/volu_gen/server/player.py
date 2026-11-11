"""The engine mode, the play queue and Studio, as engine commands
(docs/architecture.md → Main flows 2, 5, 7; docs/ui.md → Engine modes).

The engine does not know the mode: it gets `load`s. The server keeps what
it told the engine to hold in each slot (`slots`), and from that decides
what to send:
- **Playback** plays the queue. The playing track (`current`) is out of
  the queue: it leaves the queue when it is loaded into the current slot
  or when the engine reports `track_started` for it from the next slot.
  `next` is the first queued track whose active version is playable;
  unplayable ones keep their place at the front (Main flows 2).
  `reconcile_next` re-checks this after anything that can change it.
- **Studio** plays the selected version of the Studio track, from 0 when
  the track opens. Leaving Studio loads the playback track again where it
  was, paused (Main flows 7).

Engine semantics this relies on (docs/architecture.md → IPC):
`load(current)` replaces the current slot and keeps the transport state;
on a stopped engine it leaves it paused at `start`. `stop` empties both
slots. The engine sends `released` for every animation it closes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from ..ipc import ClearNext, EngineHello, Load, Pause, Play, Seek, Status, Stop, SwapAnim

if TYPE_CHECKING:
    from .app import Server

Mode = Literal["playback", "studio"]


class NotPlayable(Exception):
    """Nothing playable: no rendered version at the display FPS."""


@dataclass(frozen=True)
class Loaded:
    """A slot as the server filled it. pos: the play-queue entry it came
    from (a next slot), else None."""
    track_id: int
    version_id: int
    audio_path: str
    anim_path: str
    pos: int | None = None

    def same_file(self, other: Loaded | None) -> bool:
        return other is not None and (self.track_id, self.anim_path, self.pos) == \
            (other.track_id, other.anim_path, other.pos)


class Player:
    def __init__(self, server: Server):
        self.s = server
        self.mode: Mode = "playback"
        self.current: int | None = None     # the playback track (out of the queue), loaded or not
        self.position = 0.0                 # s into it (kept while in Studio)
        self.studio_track: int | None = None
        self.studio_version: int | None = None   # None: the draft if rendered, else the active version
        self.studio_position = 0.0
        self.slots: dict[str, Loaded] = {}
        self.state = "stopped"              # the engine's, from status

    # -- what to load
    def entry(self, track_id: int, version_id: int | None = None, pos: int | None = None) -> Loaded | None:
        """The track's given (default: active) version, if playable."""
        lib = self.s.library
        try:
            row = lib.track(track_id)
            vid = version_id if version_id is not None else row["active_version_id"]
            if vid is None:
                return None
            v = lib.version(vid)
        except KeyError:
            return None
        if v["track_id"] != track_id or not lib.playable(v, self.s.settings.display_fps):
            return None
        audio = lib.audio_path(row["key"])
        return None if audio is None else Loaded(track_id, vid, audio, v["anim_path"], pos)

    def queue(self) -> list[tuple[int, int]]:
        return [(r["pos"], r["track_id"]) for r in self.s.db.execute("SELECT pos, track_id FROM play_queue "
                                                                      "ORDER BY pos")]

    def first_playable(self) -> Loaded | None:
        for pos, track_id in self.queue():
            if (e := self.entry(track_id, pos=pos)) is not None:
                return e
        return None

    def studio_entry(self) -> Loaded | None:
        if self.studio_track is None:
            return None
        if self.studio_version is not None:
            return self.entry(self.studio_track, self.studio_version)
        d = self.s.library.draft(self.studio_track)
        return (d is not None and self.entry(self.studio_track, d["id"])) or self.entry(self.studio_track)

    def _dequeue(self, pos: int | None):
        if pos is not None:
            with self.s.db:
                self.s.db.execute("DELETE FROM play_queue WHERE pos = ?", (pos,))
            self.s.hub.changed("queue")

    # -- engine commands
    def _send(self, msg) -> bool:
        return self.s.engine_send(msg)

    def _load(self, slot: str, e: Loaded, start: float = 0.0) -> bool:
        if self._send(Load(slot=slot, track_id=e.track_id, audio_path=e.audio_path, anim_path=e.anim_path,
                           start=start)):
            self.slots[slot] = e
            return True
        return False

    def _clear_next(self):
        if self.slots.pop("next", None) is not None:
            self._send(ClearNext())

    def _stop(self):
        self._send(Stop())
        self.slots.clear()

    def reconcile_next(self):
        """Make the next slot hold the first playable queued track (Main
        flows 2): a track that became ready replaces a later one."""
        if self.mode != "playback" or "current" not in self.slots:
            if self.mode == "studio":
                self._clear_next()
            return
        want = self.first_playable()
        if want is not None and want.same_file(self.slots.get("next")):
            return
        self._clear_next()
        if want is not None:
            self._load("next", want)
        self.s.state_changed()

    # -- transport (Main flows 5)
    def play(self):
        if "current" not in self.slots:
            if self.mode == "studio":
                e = self.studio_entry()
                if e is None or not self._load("current", e, 0.0):
                    raise NotPlayable("the Studio track has no version rendered at the display FPS")
            else:
                self._load_playback(0.0)
        self._send(Play())
        self.s.state_changed()

    def _load_playback(self, start: float):
        """Load the playback track (or, if it isn't playable, the first
        playable queued one) and its next."""
        e = self.entry(self.current) if self.current is not None else None
        if e is None:
            e, start = self.first_playable(), 0.0
            if e is None:
                self.current = None
                raise NotPlayable("nothing in the queue is rendered at the display FPS")
            self._dequeue(e.pos)
            e = replace(e, pos=None)
            self.current = e.track_id
        self.position = start
        if self._load("current", e, start):
            self.reconcile_next()

    def pause(self):
        self._send(Pause())

    def stop(self):
        self._stop()
        if self.mode == "playback":
            self.position = 0.0
        else:
            self.studio_position = 0.0
        self.s.state_changed()

    def seek(self, t: float):
        self._send(Seek(t=t))

    def next(self):
        """Skip to the next playable queued track (Playback)."""
        if self.mode != "playback":
            raise NotPlayable("Next is for the play queue; Studio plays one track")
        e = self.first_playable()
        if e is None:
            self.current = None
            self._stop()
            self.s.state_changed()
            return
        self._dequeue(e.pos)
        self.current, self.position = e.track_id, 0.0
        if self._load("current", replace(e, pos=None)):
            self.reconcile_next()
        self.s.state_changed()

    def play_now(self, track_id: int):
        """Library "Play now": the track replaces the playing one at once;
        the queue stays as it is."""
        e = self.entry(track_id)
        if e is None:
            raise NotPlayable(f"track {track_id} has no active version rendered at the display FPS")
        if self.mode == "studio":
            self.mode = "playback"
            self._clear_next()
        self.current, self.position = track_id, 0.0
        if self._load("current", e):
            self.reconcile_next()
            self._send(Play())
        self.s.state_changed()

    # -- modes (Main flows 7)
    def set_mode(self, mode: Mode):
        if mode == self.mode:
            return
        self._send(Pause())
        self.mode = mode
        if mode == "studio":
            self._clear_next()
            self._load_studio(0.0)
        else:
            try:
                self._load_playback(self.position)
            except NotPlayable:
                self._stop()
        self.s.state_changed()

    def _load_studio(self, start: float):
        e = self.studio_entry()
        self.studio_position = start
        if e is None:
            self._stop()
        else:
            self._load("current", e, start)

    def open_studio(self, track_id: int | None, version_id: int | None = None):
        """Open a track in Studio (from 0), or select another version of it
        (at the same playhead)."""
        same = track_id == self.studio_track
        self.studio_track, self.studio_version = track_id, version_id
        if self.mode == "studio":
            if not same:
                self._send(Pause())
            self._load_studio(self.studio_position if same else 0.0)
        self.s.state_changed()

    # -- engine events
    def on_status(self, st: Status):
        self.state = st.state
        cur = self.slots.get("current")
        if cur is not None and st.track_id == cur.track_id and st.position is not None:
            if self.mode == "playback":
                self.position = st.position
            else:
                self.studio_position = st.position

    def on_track_started(self, track_id: int):
        nxt = self.slots.get("next")
        if nxt is None or nxt.track_id != track_id:
            return
        del self.slots["next"]
        self.slots["current"] = replace(nxt, pos=None)
        self.current, self.position = track_id, 0.0
        self._dequeue(nxt.pos)
        self.reconcile_next()
        self.s.state_changed()

    def on_track_ended(self, track_id: int):
        cur = self.slots.get("current")
        if "next" in self.slots or cur is None or cur.track_id != track_id:
            return
        del self.slots["current"]
        if self.mode == "playback":
            self.current, self.position = None, 0.0
        else:
            self.studio_position = 0.0
        self.s.state_changed()

    def on_hello(self, hello: EngineHello):
        """After `configure`. A server that restarted adopts what the engine
        plays; an engine that restarted (or lost our loads) gets the
        current track again, paused where it was (Failure handling)."""
        held = {s.slot: s for s in hello.slots}
        mine = {k: (v.track_id, v.anim_path) for k, v in self.slots.items()}
        if mine == {k: (v.track_id, v.anim_path) for k, v in held.items()} and (mine or not self._wanted()):
            return
        if not mine and not self._wanted():
            self._adopt(held, hello.position)
        else:
            self._restore(held)
        self.s.state_changed()

    def _wanted(self) -> bool:
        """Something should be loaded in the current mode."""
        return (self.studio_track if self.mode == "studio" else self.current) is not None

    def _adopt(self, held: dict, position: float | None):
        db = self.s.db
        for slot, h in held.items():
            v = db.execute("SELECT v.id FROM versions v JOIN tracks t ON t.id = v.track_id WHERE v.track_id = ? "
                           "AND v.anim_path = ? ORDER BY v.id = t.active_version_id DESC, v.id",
                           (h.track_id, h.anim_path)).fetchone()
            if v is None:
                continue
            pos = next((p for p, t in self.queue() if t == h.track_id), None) if slot == "next" else None
            self.slots[slot] = Loaded(h.track_id, v["id"], h.audio_path, h.anim_path, pos)
        if "current" in self.slots:
            self.current, self.position = self.slots["current"].track_id, position or 0.0
        self.reconcile_next()

    def _restore(self, held: dict):
        self.slots.clear()
        self._send(Pause())
        if "next" in held:
            self._send(ClearNext())
        try:
            if self.mode == "studio":
                self._load_studio(self.studio_position)
            elif self.current is not None:
                self._load_playback(self.position)
        except NotPlayable:
            pass

    def engine_lost(self):
        """The engine process is gone: it holds nothing now."""
        self.slots.clear()
        self.state = "stopped"
        self.s.state_changed()

    # -- library events
    def on_render_done(self, version_id: int):
        """A version got a new file: a loaded one switches at the playhead
        (swap_anim, Main flows 3); a newly playable one may become next."""
        cur = self.slots.get("current")
        if cur is not None and cur.version_id == version_id:
            e = self.entry(cur.track_id, version_id)
            if e is not None and e.anim_path != cur.anim_path and self._send(SwapAnim(anim_path=e.anim_path)):
                self.slots["current"] = replace(cur, anim_path=e.anim_path)
        elif cur is None and self.mode == "studio" and self.studio_track is not None:
            self._load_studio(0.0)
        self.reconcile_next()

    def fps_changed(self):
        """Every loaded file is at the old rate (Main flows 6)."""
        self._stop()
        self.position = self.studio_position = 0.0

    def track_removed(self, track_id: int):
        if self.current == track_id:
            self.current, self.position = None, 0.0
        if self.studio_track == track_id:
            self.studio_track = self.studio_version = None
        self.reconcile_next()
        self.s.state_changed()

    def loads(self, track_id: int) -> bool:
        cur = self.slots.get("current")
        return cur is not None and cur.track_id == track_id

    # -- for the UI
    def snapshot(self) -> dict:
        def slot(name):
            e = self.slots.get(name)
            return None if e is None else {"track_id": e.track_id, "version_id": e.version_id}
        return {"mode": self.mode, "current_track_id": self.current, "position": self.position,
                "studio": {"track_id": self.studio_track, "version_id": self.studio_version,
                           "position": self.studio_position},
                "slots": {"current": slot("current"), "next": slot("next")}}
