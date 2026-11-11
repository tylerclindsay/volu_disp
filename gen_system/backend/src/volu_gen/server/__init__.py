"""The API server (docs/architecture.md → API server): the coordinator and
the only holder of persistent state. The UI talks only to it.

Built so far: the SQLite DB, settings, the library (import, removal,
storage), analysis and peaks for the UI, planning, versions, presets, the
play queue and the job queue. Still to come: running jobs on the worker,
the engine link and supervision, and the WebSocket.
"""

from .app import Server, create_app
from .display import Display

__all__ = ["Display", "Server", "create_app"]
