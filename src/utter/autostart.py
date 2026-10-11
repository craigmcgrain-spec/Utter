"""Start-at-login: the config key is the intent, the desktop file its materialisation.

``[general].autostart`` is the one source of truth for whether Utter starts at login, so every
reader and writer goes through the functions here. ``~/.config/autostart/utter.desktop`` is
derived from the key -- created from :data:`ENTRY` when the key is true, removed when it is
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
import re
from pathlib import Path

from .config import Config
from .paths import config_home, venv_dir

logger = logging.getLogger(__name__)

ENTRY = """\
[Desktop Entry]
Type=Application
Name=Utter
Comment=Speak the current selection
Exec=@EXEC@
X-GNOME-Autostart-enabled=true
"""
"""The autostart entry's template; ``@EXEC@`` is the launcher :func:`launcher_command` names."""

_SAFE_EXEC = re.compile(r"^[A-Za-z0-9_./:=+,@-]+$")
"""An ``Exec`` value the Desktop Entry spec needs no quoting for; anything else is quoted."""


def autostart_path() -> Path:
    """Where the start-at-login entry lives, honouring the XDG override."""
    return config_home() / "autostart" / "utter.desktop"


def launcher_command() -> str:
    """The command the autostart entry runs: this machine's installed launcher.

    The installer writes the venv binary's absolute path into the entry it creates, and for good
    reason: ``Exec=utter`` alone is resolved through the *login session's* PATH, which is not
    guaranteed to carry ``~/.local/bin`` (the installer's shim) or a ``--prefix`` install's bin
    directory, so an entry that only names ``utter`` can silently fail to start at login. The
    tray writes the same thing when it creates the entry, which is what makes disabling and
    re-enabling start-at-login keep a launcher that does not depend on PATH;
    ``utter`` is the fallback for a checkout run with no installed venv.
    """
    binary = venv_dir() / "bin" / "utter"
    return str(binary) if binary.exists() else "utter"


def exec_value(command: str) -> str:
    """``command`` as an ``Exec`` key's value, quoted per the Desktop Entry spec.

    ``Exec`` is parsed as an argv, so a path carrying a space -- a home directory or a
    ``--prefix`` with one -- has to be quoted, or the session runs a different program (or drops
    the entry altogether); a double quote, backslash, backtick or dollar inside the quotes is
    escaped, and a literal percent is doubled because ``Exec`` reserves it for field codes. This
    mirrors ``install.sh``'s ``desktop_exec_value`` so the entry the tray writes and the entry
    the installer writes are quoted identically.
    """
    doubled = command.replace("%", "%%")
    if _SAFE_EXEC.match(doubled):
        return doubled
    escaped = (
        doubled.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("`", "\\`")
        .replace("$", "\\$")
    )
    return f'"{escaped}"'


def entry_text() -> str:
    """The autostart entry's content: :data:`ENTRY` with its launcher filled in."""
    return ENTRY.replace("@EXEC@", exec_value(launcher_command()))


def reconcile_autostart(config: Config, notifier: object | None) -> bool:
    """Make the autostart file's existence match ``config.general.autostart``; return success.

    The key is the intent and the file is derived from it, so a checked item whose write failed
    is still "on" and the next startup retries. Reconciliation is idempotent rather than a
    rewrite: a file already present is left exactly as it is, which is what keeps the
    installer's entry -- with its ``TryExec``, icon and desktop-integration keys -- instead of
    replacing it with the tray's own minimal copy. That copy is not generic in the one place it
    matters, though: :func:`entry_text` names the same absolute launcher the installer does, so an
    entry created here still starts without a PATH that carries ``~/.local/bin``.
    ``notifier`` is the tray icon a failure is shown on (``None`` drops the notice, which is what
    a headless caller passes on purpose); the failure is logged either way. ``notify`` is
    imported lazily so that importing this module -- which the headless ``doctor`` does -- never
    drags ``PyQt6`` in.
    """
    entry = autostart_path()
    try:
        if config.general.autostart:
            if not entry.exists():
                entry.parent.mkdir(parents=True, exist_ok=True)
                entry.write_text(entry_text(), encoding="utf-8")
        else:
            entry.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("could not update %s: %s", entry, exc)
        from .ui.notify import notify  # noqa: PLC0415 — see the docstring

        notify(notifier, f"Could not update the autostart entry: {exc}")
        return False
    return True
