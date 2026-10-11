"""Start-at-login: the config key is the intent, the desktop file its materialisation.

``[general].autostart`` is the one source of truth for whether Utter starts at login, so every
reader and writer goes through the functions here. ``~/.config/autostart/utter.desktop`` is
derived from the key -- created with :data:`ENTRY` when the key is true, removed when it is
false -- rather than being a second place the answer lives. The tray item reads and writes the
key, the app reconciles the file from it at startup, and the installer records the key when it
answers the same question; without that, setting the key by hand would do nothing, which is
exactly the dead-key defect this module removes.

A key that cannot be honoured -- an autostart directory that cannot be created or written, a
file that cannot be removed -- is reported through ``notifier`` (and logged) instead of being
swallowed, because a login that never happens is not something a silent ``except`` should hide.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .config import Config
from .paths import config_home

logger = logging.getLogger(__name__)

ENTRY = """\
[Desktop Entry]
Type=Application
Name=Utter
Comment=Speak the current selection
Exec=utter
X-GNOME-Autostart-enabled=true
"""
"""The autostart entry's content; ``Exec=utter`` resolves through the user's PATH."""


def autostart_path() -> Path:
    """Where the start-at-login entry lives, honouring the XDG override."""
    return config_home() / "autostart" / "utter.desktop"


def reconcile_autostart(config: Config, notifier: object | None) -> bool:
    """Make the autostart file's existence match ``config.general.autostart``; return success.

    The key is the intent and the file is derived from it, so a checked item whose write failed
    is still "on" and the next startup retries. Reconciliation is idempotent rather than a
    rewrite: a file already present is left exactly as it is, which is what keeps a prefixed
    install's entry pointing at its own venv binary instead of clobbering it with the generic
    ``Exec=utter`` line the toggle writes. ``notifier`` is the tray icon a failure is shown on
    (``None`` drops the notice, which is what a headless caller passes on purpose); the failure
    is logged either way. ``notify`` is imported lazily because the tray module imports this
    one, so a top-level import here would be circular.
    """
    entry = autostart_path()
    try:
        if config.general.autostart:
            if not entry.exists():
                entry.parent.mkdir(parents=True, exist_ok=True)
                entry.write_text(ENTRY, encoding="utf-8")
        else:
            entry.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("could not update %s: %s", entry, exc)
        from .ui.notify import notify  # noqa: PLC0415 — see the docstring

        notify(notifier, f"Could not update the autostart entry: {exc}")
        return False
    return True
