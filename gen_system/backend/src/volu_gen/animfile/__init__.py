"""Animation file (.anim) format: decision 0008."""

from .format import (CHUNK_FRAMES, FILTER_DELTA, FILTER_NONE, FORMAT_VERSION, AnimFormatError,
                     AnimReader, AnimWriter, chunk_count, tmp_path)

__all__ = ["AnimFormatError", "AnimReader", "AnimWriter", "CHUNK_FRAMES", "FORMAT_VERSION",
           "FILTER_DELTA", "FILTER_NONE", "chunk_count", "tmp_path"]
