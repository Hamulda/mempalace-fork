"""Utility functions for the application."""
from typing import Any


def sanitize_input(value: str) -> str:
    """Strip whitespace and control characters from user input."""
    return "".join(c for c in value if c.isprintable()).strip()


def format_error(message: str, code: int = 500) -> dict[str, Any]:
    """Format a JSON error response."""
    return {"error": message, "code": code}


def deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge overlay into base dict."""
    result = base.copy()
    for k, v in overlay.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result
