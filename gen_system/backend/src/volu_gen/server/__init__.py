"""The API server (docs/architecture.md → API server): the coordinator and
the only holder of persistent state. The UI talks only to it.

It holds the SQLite DB, the settings, the library, planning, versions,
presets and both queues; dispatches jobs to the worker and applies their
results; drives the engine (configure, the play queue, Studio, swap_anim);
starts and restarts both children; detects drive nodes by ping; cleans up
animation files nothing uses; and pushes live state over a WebSocket.
"""

from . import cleanup
from .app import create_app
from .core import Runtime, Server
from .display import Display

__all__ = ["Display", "Runtime", "Server", "cleanup", "create_app"]
