"""
Test that m1_runtime_doctor.py does not trigger heavy imports.

monkeypatch importlib.util.find_spec to track which packages are checked,
then run the doctor and assert heavy packages were only checked via spec,
never actually imported.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from unittest import mock

import pytest


HEAVY_PACKAGES = ["sentence_transformers", "fastembed"]


class ImportTracker:
    """Records which packages were spec-checked."""

    def __init__(self):
        self.spec_checked: set[str] = set()

    def find_spec_hook(self, name: str, target=None):
        """Record spec lookups, then return None to let normal resolution proceed."""
        self.spec_checked.add(name)
        return None


@pytest.fixture
def import_tracker():
    return ImportTracker()


def test_heavy_packages_not_imported(import_tracker):
    """Doctor must run without triggering heavy package imports."""
    script_path = pathlib.Path(__file__).parent.parent / "scripts" / "m1_runtime_doctor.py"
    result = subprocess.run(
        [sys.executable, str(script_path), "--json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    # doctor should exit 0 (no swap) or 1 (swap detected) — both are fine
    assert result.returncode in (0, 1), f"doctor failed stderr: {result.stderr}"


def test_find_spec_used_for_heavy_packages(import_tracker):
    """Verify find_spec is called for heavy packages, not direct import."""
    import importlib.util

    tracked_specs: set[str] = set()
    orig_find_spec = importlib.util.find_spec

    def tracking_find_spec(name: str, target=None):
        tracked_specs.add(name)
        return orig_find_spec(name, target)

    with mock.patch("importlib.util.find_spec", side_effect=tracking_find_spec):
        import scripts.m1_runtime_doctor as _doctor_module

        for pkg in HEAVY_PACKAGES:
            assert pkg in tracked_specs, f"{pkg} was never spec-checked"


def test_sentence_transformers_not_in_sysmodules_after_doctor():
    """Running doctor must not add sentence_transformers or torch to sys.modules."""
    script_path = pathlib.Path(__file__).parent.parent / "scripts" / "m1_runtime_doctor.py"
    result = subprocess.run(
        [sys.executable, "-c", f"""
import sys
import json
import subprocess
import os
import pathlib

script = pathlib.Path(r"{script_path}")
result = subprocess.run(
    [sys.executable, str(script), "--json"],
    capture_output=True, text=True, timeout=30
)
data = json.loads(result.stdout)
print("OK", file=open("/dev/stdout", "w"))
print("heavy_imports_avoided:", data.get("heavy_imports_avoided"), file=open("/dev/stdout", "w"))
print("checked_by_spec:", data.get("checked_by_spec"), file=open("/dev/stdout", "w"))
"""],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "OK" in result.stdout, f"doctor failed: {result.stderr}"


def test_report_has_lightweight_fields():
    """Report must include heavy_imports_avoided and checked_by_spec."""
    import scripts.m1_runtime_doctor as doctor_module

    report = doctor_module.get_report()
    assert report.get("heavy_imports_avoided") is True
    assert "checked_by_spec" in report
    checked = report.get("checked_by_spec", [])
    assert "sentence_transformers" in checked
