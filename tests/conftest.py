"""Shared fixtures: every path-touching test runs against a temporary XDG root."""

import pytest


@pytest.fixture
def tmp_xdg(tmp_path, monkeypatch):
    """Point every UTTER_* XDG override at a fresh temporary root and return it."""
    root = tmp_path / "xdg"
    overrides = {
        "UTTER_CONFIG_HOME": "config",
        "UTTER_STATE_HOME": "state",
        "UTTER_DATA_HOME": "data",
        "UTTER_RUNTIME_DIR": "runtime",
    }
    for var, subdir in overrides.items():
        directory = root / subdir
        directory.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(var, str(directory))
    return root
