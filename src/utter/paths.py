"""XDG path resolution, with UTTER_* overrides for tests and --prefix installs."""

import os
from pathlib import Path


def _xdg(override: str, xdg_var: str, default: str) -> Path:
    """Resolve a base directory: UTTER_* override, then XDG var, then the XDG default."""
    for candidate in (os.environ.get(override), os.environ.get(xdg_var)):
        if candidate:
            return Path(candidate).expanduser()
    return Path(default).expanduser()


def config_home() -> Path:
    return _xdg("UTTER_CONFIG_HOME", "XDG_CONFIG_HOME", "~/.config")


def state_home() -> Path:
    return _xdg("UTTER_STATE_HOME", "XDG_STATE_HOME", "~/.local/state")


def data_home() -> Path:
    return _xdg("UTTER_DATA_HOME", "XDG_DATA_HOME", "~/.local/share")


def _runtime_dir() -> Path:
    runtime = os.environ.get("UTTER_RUNTIME_DIR") or os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime).expanduser()
    return Path(f"/tmp/utter-{os.getuid()}")


def config_path() -> Path:
    return config_home() / "utter" / "config.toml"


def log_path() -> Path:
    return state_home() / "utter" / "utter.log"


def models_dir() -> Path:
    return data_home() / "utter" / "models"


def venv_dir() -> Path:
    return data_home() / "utter" / "venv"


def socket_path() -> Path:
    return _runtime_dir() / "utter.sock"
