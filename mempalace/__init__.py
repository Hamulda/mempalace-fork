"""MemPalace — Give your AI a memory. No API key required."""

import logging
import os
import platform

from .cli import main  # noqa: E402
from .version import __version__  # noqa: E402

# ONNX Runtime's CoreML provider segfaults during vector queries on Apple Silicon.
# Force CPU execution unless the user has explicitly set a preference.
if platform.machine() == "arm64" and platform.system() == "Darwin":
    os.environ.setdefault("ORT_DISABLE_COREML", "1")

# Chroma telemetry silencing — only loaded when Chroma backend is actually used.
# This is now lazy, triggered by backends/__init__.py get_backend("chroma").
def _silence_chroma_telemetry():
    logging.getLogger("chromadb.telemetry.product.posthog").setLevel(logging.CRITICAL)

__all__ = ["main", "__version__", "_silence_chroma_telemetry"]
