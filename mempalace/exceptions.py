"""
MemPalace exceptions.
"""

class MemoryPressureError(RuntimeError):
    """Raised when system memory pressure prevents safe palace writes."""
    pass