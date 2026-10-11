"""Turn the desktop's current selection into clean text for the synthesiser.

The Primary Selection is tried first and the clipboard is the fallback, because
Electron, VS Code and Wayland-native Firefox frequently do not publish a Primary
Selection (spec §5). On Wayland the primary is read with ``wl-paste --primary
--no-newline``; on X11 it is Qt's ``QClipboard.Selection``. Both paths degrade to
``None`` — a machine without wl-clipboard, a Qt-less machine or a non-zero exit
means "nothing selected", never a crash: the tray has to survive all of them.

This module is the last line of defence between arbitrary desktop bytes and the
engine, so :func:`sanitize` strips NULs and control characters and treats a
whitespace-only selection as nothing selected, and :func:`read_selection` refuses
a sanitised selection over ``max_chars`` with :class:`SelectionTooLong` (spec §5).

``PyQt6`` is imported lazily inside the X11 path only, so importing this module
never pulls Qt and never needs a display.
"""

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable

# How long wl-paste may block before the press counts as "nothing selected".
WL_PASTE_TIMEOUT = 2.0

# NUL, the C0 controls that are not whitespace, DEL and the C1 controls. Tab,
# newline and carriage return are deliberately absent: they are whitespace.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_WHITESPACE = re.compile(r"\s+")


class SelectionTooLong(ValueError):
    """The sanitised selection exceeds ``max_chars`` and is refused."""


@dataclass(frozen=True)
class Selection:
    """A captured selection and the provider that supplied it (``primary``/``clipboard``)."""

    text: str
    source: str


def sanitize(raw: str | None) -> str | None:
    """Clean a raw selection, or return ``None`` when nothing is selected.

    Strips NULs and control characters, collapses runs of whitespace (which also
    removes the trailing newline), and rejects empty or whitespace-only input.
    """
    if not raw:
        return None
    text = _WHITESPACE.sub(" ", _CONTROL.sub("", raw)).strip()
    return text or None


def _qt_text(selection: bool) -> str | None:
    """Read Qt's primary selection or clipboard text; ``None`` when Qt is unusable.

    ``PyQt6`` is imported here rather than at module level so that importing this
    module needs neither Qt nor a display. Any failure — no Qt, no clipboard —
    means "nothing selected".
    """
    try:
        from PyQt6.QtGui import QClipboard
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            return None
        mode = QClipboard.Mode.Selection if selection else QClipboard.Mode.Clipboard
        return app.clipboard().text(mode)
    except Exception:
        return None


def _wayland_primary() -> str | None:
    """Run ``wl-paste --primary --no-newline``; ``None`` on any failure."""
    try:
        result = subprocess.run(
            ["wl-paste", "--primary", "--no-newline"],
            capture_output=True,
            text=True,
            timeout=WL_PASTE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        # Missing binary, no Wayland session, permission error or a timeout.
        return None
    if getattr(result, "returncode", 0) != 0:
        return None
    return result.stdout


def _x11_primary() -> str | None:
    """Read Qt's X11 Primary Selection through a lazy import."""
    return _qt_text(selection=True)


def _clipboard() -> str | None:
    """Read Qt's regular clipboard text through a lazy import."""
    return _qt_text(selection=False)


def _nothing() -> str | None:
    """The reader for session types with no supported primary selection."""
    return None


def default_primary(session_type: str) -> Callable[[], str | None]:
    """The primary-selection reader for ``session_type``.

    Unknown session types get a provider that always yields ``None``.
    """
    return {
        "wayland": _wayland_primary,
        "x11": _x11_primary,
    }.get(session_type.lower(), _nothing)


def read_selection(
    *,
    session_type: str | None = None,
    primary: Callable[[], str | None] | None = None,
    clipboard: Callable[[], str | None] | None = None,
    max_chars: int = 20000,
) -> Selection | None:
    """Read the current selection, or return ``None`` when there is nothing to read.

    Injected ``primary``/``clipboard`` providers win; otherwise they are built
    from ``session_type`` (or from ``XDG_SESSION_TYPE`` when it is not given).
    The clipboard is consulted only when the primary sanitises to nothing, and
    ``source`` records which provider supplied the text.

    Raises :class:`SelectionTooLong` when the *sanitised* text exceeds
    ``max_chars``, so the caller can refuse it with a notification.
    """
    if primary is None:
        if session_type is None:
            session_type = os.environ.get("XDG_SESSION_TYPE", "")
        primary = default_primary(session_type)
    if clipboard is None:
        clipboard = _clipboard

    for provider, source in ((primary, "primary"), (clipboard, "clipboard")):
        text = sanitize(provider())
        if text is None:
            continue
        if len(text) > max_chars:
            raise SelectionTooLong(
                "selection is %d characters, over the %d limit" % (len(text), max_chars)
            )
        return Selection(text, source)
    return None
