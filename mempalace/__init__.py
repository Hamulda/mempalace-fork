"""MemPalace — Give your AI a memory. No API key required."""

import os
import platform

from .cli import main  # noqa: E402
from .version import __version__  # noqa: E402

# ONNX Runtime's CoreML provider segfaults during vector queries on Apple Silicon.
# Force CPU execution unless the user has explicitly set a preference.
if platform.machine() == "arm64" and platform.system() == "Darwin":
    os.environ.setdefault("ORT_DISABLE_COREML", "1")

__all__ = ["main", "__version__"]
