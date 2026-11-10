"""The methods (docs/generators.md → Methods). Importing this package
registers each one in `METHODS`."""

from .galaxy import Galaxy
from .geometry import Shapes
from .raindrops import Raindrops
from .strobe import Strobe

__all__ = ["Galaxy", "Raindrops", "Shapes", "Strobe"]
