#!/usr/bin/env python3
"""Import convergence check for mempalace-fork."""
import sys

EXPECTED_SUBSTRING = ".claude/plugins/marketplaces/mempalace-fork/mempalace"

def check():
    try:
        import mempalace
        pkg_file = mempalace.__file__
    except ImportError:
        print("FAIL: mempalace not importable")
        return 1

    if EXPECTED_SUBSTRING not in pkg_file:
        print(f"FAIL: mempalace.__file__ = {pkg_file!r}")
        print(f"  does not contain {EXPECTED_SUBSTRING!r}")
        return 1

    print(f"OK: {pkg_file}")
    return 0

if __name__ == "__main__":
    sys.exit(check())
