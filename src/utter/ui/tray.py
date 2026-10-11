"""The system-tray icon and the menu that is the app's control surface.

Every item delegates immediately: nothing here caches playback state. The voice and
speed items write the shared :class:`~utter.config.Speech` object the engine already
holds, so the change is live for the next utterance, and persist it with
``Config.save()``. The start-at-login item derives its initial check from the file on
disk and makes the file match the action on every toggle.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from PyQt6.QtGui import QAction, QActionGroup
from PyQt6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon

from ..capture import SelectionTooLong, read_selection
from ..config import Config
from ..paths import config_home
from ..tts.chunk import TooLongError
from .notify import notify

if TYPE_CHECKING:
    # Annotation only: importing the engine at runtime would drag numpy and the audio
    # stack into every module that imports the tray, and the tray needs only the interface.
    from ..tts.engine import SynthService

logger = logging.getLogger(__name__)

SPEEDS = (0.75, 1.0, 1.25, 1.5, 2.0)
"""The speed steps the menu offers; the engine accepts anything, these are the presets."""

_AUTOSTART_ENTRY = """\
[Desktop Entry]
Type=Application
Name=Utter
Comment=Speak the current selection
Exec=utter
X-GNOME-Autostart-enabled=true
"""
"""What the start-at-login toggle writes; ``Exec=utter`` resolves through the user's PATH."""


def autostart_path() -> Path:
    """Where the start-at-login entry lives, honouring the XDG override."""
    return config_home() / "autostart" / "utter.desktop"


def _toggle_autostart(checked: bool, notifier: QSystemTrayIcon | None) -> None:
    """Write the autostart entry when checked, remove it when unchecked.

    A read-only or full config home reports through ``notify`` instead of raising: this
    runs in a Qt slot, where an escaping exception is a crash.
    """
    entry = autostart_path()
    try:
        if checked:
            entry.parent.mkdir(parents=True, exist_ok=True)
            entry.write_text(_AUTOSTART_ENTRY, encoding="utf-8")
        else:
            entry.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("could not update %s: %s", entry, exc)
        notify(notifier, f"Could not update the autostart entry: {exc}")


def _persist(config: Config, notifier: QSystemTrayIcon | None) -> None:
    """Save the chosen setting, reporting an unwritable config file rather than raising."""
    try:
        config.save()
    except OSError as exc:
        logger.warning("could not save config: %s", exc)
        notify(notifier, f"Could not save settings: {exc}")


def _set_voice(config: Config, voice: str, notifier: QSystemTrayIcon | None) -> None:
    config.speech.voice = voice  # the same object the engine reads per unit
    _persist(config, notifier)


def _set_speed(config: Config, speed: float, notifier: QSystemTrayIcon | None) -> None:
    config.speech.speed = speed
    _persist(config, notifier)


def _speak(config: Config, service: SynthService, notifier: QSystemTrayIcon | None) -> None:
    """Capture the selection and hand it to the engine, reporting any refusal.

    Both refusals are caught: capture raises :class:`SelectionTooLong` for a sanitised
    selection over the limit, and the engine raises :class:`TooLongError` when
    normalisation changes the length. Either escaping the slot would crash the app.
    """
    try:
        selection = read_selection(max_chars=config.speech.max_chars)
        if selection is None:
            notify(notifier, "No text selected")
            return
        service.speak(selection.text)
    except (SelectionTooLong, TooLongError) as exc:
        notify(notifier, str(exc))


def build_menu(
    config: Config,
    service: SynthService,
    on_quit: Callable[[], None],
    notifier: QSystemTrayIcon | None,
) -> QMenu:
    """Build the tray menu: speak, pause/resume, stop, voice, speed, autostart, quit.

    ``notifier`` is the icon refusals are shown on, and is required: :class:`Tray`
    passes itself, and a caller with no tray passes ``None`` explicitly rather than
    inheriting a default that would silently swallow every refusal.
    """
    menu = QMenu()

    menu.addAction("Speak selection").triggered.connect(
        lambda: _speak(config, service, notifier)
    )

    pause = menu.addAction("Pause / Resume")
    pause.setObjectName("pauseAction")  # so Tray.set_state can find it to sync
    pause.setCheckable(True)
    pause.setChecked(bool(service.status().get("paused", False)))

    def toggle_pause(checked: bool) -> None:
        service.pause() if checked else service.resume()

    pause.triggered.connect(toggle_pause)

    menu.addAction("Stop").triggered.connect(service.stop)

    voices = menu.addMenu("Voice")
    voice_group = QActionGroup(voices)
    for name in service.voice_names():
        item = voices.addAction(name)
        item.setCheckable(True)
        item.setChecked(name == config.speech.voice)
        item.setActionGroup(voice_group)
        item.triggered.connect(
            lambda _=False, name=name: _set_voice(config, name, notifier)
        )

    speeds = menu.addMenu("Speed")
    speed_group = QActionGroup(speeds)
    for speed in SPEEDS:
        item = speeds.addAction(f"{speed:g}x")
        item.setCheckable(True)
        item.setChecked(abs(speed - config.speech.speed) < 1e-9)
        item.setActionGroup(speed_group)
        item.triggered.connect(
            lambda _=False, speed=speed: _set_speed(config, speed, notifier)
        )

    autostart = menu.addAction("Start at login")
    autostart.setCheckable(True)
    autostart.setChecked(autostart_path().exists())
    autostart.triggered.connect(
        lambda checked=False: _toggle_autostart(checked, notifier)
    )

    menu.addSeparator()
    menu.addAction("Quit").triggered.connect(lambda: on_quit())

    return menu


class Tray(QSystemTrayIcon):
    """The tray icon: a standard volume glyph and the menu built by :func:`build_menu`."""

    def __init__(
        self, service: SynthService, config: Config, on_quit: Callable[[], None]
    ) -> None:
        super().__init__()
        self.setToolTip("Utter")
        self.setIcon(
            QApplication.style().standardIcon(QStyle.StandardPixmap.SP_MediaVolume)
        )
        self._menu = build_menu(config, service, on_quit, notifier=self)
        self.setContextMenu(self._menu)

    def set_state(self, state: str) -> None:
        """Reflect the engine's state in the tooltip and the pause item."""
        self.setToolTip(f"Utter - {state.lower()}")
        pause = self._menu.findChild(QAction, "pauseAction")
        if pause is not None:
            pause.setChecked(state.upper() == "PAUSED")
