"""XDG path resolution, with UTTER_* overrides for tests and --prefix installs."""

import os
from pathlib import Path


def _xdg(override: str, xdg_var: str, default: str) -> Path:
    """Resolve a base directory: UTTER_* override, then XDG var, then the XDG default.

    A relative value is skipped rather than used, which is what the XDG basedir spec requires
    (relative paths are invalid there) and what keeps the process's two halves in agreement: the
    tray resolves the socket path when it binds it, and ``utter speak`` resolves it again from
    its own working directory, so ``XDG_RUNTIME_DIR=runtime`` would put the two in different
    places -- which reads as "not running" and cold-starts a second engine.
    """
    for candidate in (os.environ.get(override), os.environ.get(xdg_var)):
        if not candidate:
            continue
        resolved = Path(candidate).expanduser()
        if resolved.is_absolute():
            return resolved
    return Path(default).expanduser()


def config_home() -> Path:
    return _xdg("UTTER_CONFIG_HOME", "XDG_CONFIG_HOME", "~/.config")


def state_home() -> Path:
    return _xdg("UTTER_STATE_HOME", "XDG_STATE_HOME", "~/.local/state")


def data_home() -> Path:
    return _xdg("UTTER_DATA_HOME", "XDG_DATA_HOME", "~/.local/share")


def _runtime_dir() -> Path:
    """The runtime directory, honouring the overrides, else a private per-uid one.

    ``XDG_RUNTIME_DIR`` has no default under ``$HOME`` -- the session manager sets it -- so the
    fallback is a directory under ``/tmp`` named for this uid. A relative override is skipped for
    the reason :func:`_xdg` gives.
    """
    return _xdg("UTTER_RUNTIME_DIR", "XDG_RUNTIME_DIR", f"/tmp/utter-{os.getuid()}")


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
