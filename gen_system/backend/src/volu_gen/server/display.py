"""The active display config, loaded once at server start
(`volu serve --display <config>`; switching configs takes a restart)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..geometry import MAX_FPS, DisplayConfig, Geometry, load_display_config
from ..mapping import Mapping, MappingError, fps_problems, max_fps_for


@dataclass(frozen=True, eq=False)
class Display:
    """config    the parsed config (a malformed file fails the server start)
    geometry  pixel positions and the geometry hash
    mapping   the validated mapping, or None if the config failed validation
    problems  validation problems; non-empty keeps hardware output disarmed
    """
    config: DisplayConfig
    geometry: Geometry
    mapping: Mapping | None
    problems: tuple[str, ...]

    @classmethod
    def load(cls, path: Path) -> Display:
        config = load_display_config(path)
        geometry = Geometry.from_config(config)
        try:
            return cls(config, geometry, Mapping(config, geometry), ())
        except MappingError as e:
            return cls(config, geometry, None, tuple(e.problems))

    @property
    def longest_strip(self) -> int:
        return max(n.section.height for n in self.config.drive_nodes)

    @property
    def max_fps(self) -> int:
        """Highest display FPS the longest strip allows."""
        return min(MAX_FPS, max_fps_for(self.longest_strip))

    def fps_problems(self, fps: int) -> list[str]:
        return fps_problems(fps, self.longest_strip)

    def summary(self) -> dict:
        c = self.config
        return {
            "path": str(c.path) if c.path else None,
            "size": dict(zip("xyz", c.geometry.size)),
            "pixel_count": self.geometry.pixel_count,
            "geometry_hash": self.geometry.hash,
            "default_fps": c.default_fps,
            "max_fps": self.max_fps,
            "udp_port": c.udp_port,
            "drive_nodes": [{"id": n.id, "ip": n.ip, "strips": n.section.strips,
                             "section": {"x": list(n.section.x), "y": list(n.section.y),
                                         "z": list(n.section.z)}}
                            for n in c.drive_nodes],
            "valid": not self.problems,
            "problems": list(self.problems),
        }
