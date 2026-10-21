"""Keep real AstrBot imports isolated from the user's runtime configuration."""

import os
import tempfile

import pytest

# AstrBot creates config and database files during import.
_runtime = tempfile.TemporaryDirectory(prefix="jipiao-tests-")
os.environ["ASTRBOT_ROOT"] = _runtime.name
os.environ["TEST_MODE"] = "on"


@pytest.fixture
def connecting_quote():
    """Replay a captured live itinerary; no network requests or invented fares."""
    import json
    from pathlib import Path

    report = Path(__file__).resolve().parents[1] / "validation/live_plugin_fli_2026-10-21.json"
    return json.loads(report.read_text(encoding="utf-8"))["best"]
