"""The system-tray icon and the menu that is the app's control surface.

Every item delegates immediately: nothing here caches playback state. The voice and speed items
write the shared :class:`~utter.config.Speech` object the engine already holds, so the change is
live for the next utterance, and persist it with ``Config.save()``. The start-at-login item reads
``[general].autostart`` and writes it, and the desktop file is materialised from that key by
:func:`~utter.autostart.reconcile_autostart` -- the key is the intent, so the item's check
derives from it rather than from the file's existence.

Two things here exist only to keep the two surfaces honest with each other. The voice menu groups
the pack's fifty-four ids by language and names each item for its speaker, because the flat list
was a wall of ``af_heart``-style ids; and both the voice item's check and the speed item's check
are refreshed from :meth:`Tray.set_voice` / :meth:`Tray.set_speed`, which the wiring in
:mod:`utter.ui.app` calls when the *overlay* changes either one. A speed the slider can reach but
no preset names -- 1.37, say -- is shown as a disabled ``Custom (1.37×)`` item rather than as no
selection at all.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from PyQt6.QtCore import QSize, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QIcon
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from ..autostart import reconcile_autostart
from ..capture import SelectionTooLong, read_selection
from ..config import Config
from ..tts.chunk import TooLongError
from .notify import notify
from .voices import grouped_voices, voice_description, voice_display_name, voice_label

if TYPE_CHECKING:
    # Annotation only: importing the engine at runtime would drag numpy and the audio
    # stack into every module that imports the tray, and the tray needs only the interface.
    from ..tts.engine import SynthService

logger = logging.getLogger(__name__)

SPEEDS = (0.75, 1.0, 1.25, 1.5, 2.0)
"""The speed steps the menu offers; the engine accepts anything, these are the presets."""

PLAYING = "PLAYING"
PAUSED = "PAUSED"
IDLE = "IDLE"
"""The engine's playback states, as the tray is told them."""

_PACKAGED_ICON = Path(__file__).with_name("utter.svg")
_PACKAGED_SMALL_ICON = Path(__file__).with_name("utter-small.svg")
"""The app's icon as it ships inside the package: the full drawing, and the simplified one
drawn for 16-24px, where the full drawing's letters and shading blur into the tile.

``install.sh`` copies both into the hicolor theme (scalable, and 16/22/24), so a normal install
resolves ``QIcon.fromTheme("utter")``. These copies are what a checkout run from the source tree
-- or a session whose icon cache has not caught up -- falls back to, so the tray never shows
Qt's generic volume glyph instead of the app's own icon.
"""

_SMALL_ICON_SIZES = (16, 22, 24)
_LARGE_ICON_SIZES = (32, 48, 64, 128, 256)


def app_icon() -> QIcon:
    """The app's icon: the installed theme icon, else the copies shipped in the package.

    The fallback is built from rendered sizes rather than from the two files: Qt's SVG icon
    engine keeps one file per mode and ignores the size it was added for, so the small drawing
    would never be the one picked.
    """
    icon = QIcon.fromTheme("utter")
    if not icon.isNull():
        return icon
    icon = QIcon()
    for source, sizes in (
        (QIcon(str(_PACKAGED_SMALL_ICON)), _SMALL_ICON_SIZES),
        (QIcon(str(_PACKAGED_ICON)), _LARGE_ICON_SIZES),
    ):
        for size in sizes:
            icon.addPixmap(source.pixmap(QSize(size, size)))
    return icon


def _persist(config: Config, notifier: QSystemTrayIcon | None) -> None:
    """Save the chosen setting, reporting an unwritable config file rather than raising."""
    try:
        config.save()
    except OSError as exc:
        logger.warning("could not save config: %s", exc)
        notify(notifier, f"Could not save settings: {exc}")


def _set_autostart(config: Config, checked: bool, notifier: QSystemTrayIcon | None) -> None:
    """Record the intent in the key, then materialise it as the desktop file.

    The key is written first, so an unwritable config file is reported as a failed setting
    rather than as an autostart-file problem; a file that cannot be created after the key
    changed is reported by :func:`~utter.autostart.reconcile_autostart`, and the key keeps the
    intent so the next startup retries. Both run in a Qt slot, where an escaping exception is
    a crash, so neither raises.
    """
    config.general.autostart = checked
    _persist(config, notifier)
    reconcile_autostart(config, notifier)


def _set_voice(config: Config, voice: str, notifier: QSystemTrayIcon | None) -> None:
    config.speech.voice = voice  # the same object the engine reads per unit
    _persist(config, notifier)


def _set_speed(config: Config, speed: float, notifier: QSystemTrayIcon | None) -> None:
    config.speech.speed = speed
    _persist(config, notifier)


def _set_overlay_pos(
    config: Config, position: tuple[int, int], notifier: QSystemTrayIcon | None
) -> None:
    """Remember where the overlay was dragged, so the next launch puts it back there."""
    config.ui.overlay_pos = position
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
            # The same sentence the socket's `speak` answers with, so the balloon and the CLI
            # tell a user the same thing about the same situation.
            notify(notifier, "No text to speak: nothing is selected")
            return
        service.speak(selection.text)
    except (SelectionTooLong, TooLongError) as exc:
        notify(notifier, str(exc))


def _toggle_pause(service: SynthService) -> None:
    """One item, both directions: the service's own state decides which one this is.

    The paused state is read rather than held in the item's check, because the overlay, the
    hotkey and the control socket all pause too and an item that held its own idea of the
    state would be wrong the moment one of them did.
    """
    service.resume() if service.status().get("paused") else service.pause()


def _sync_playback(pause: QAction, stop: QAction, state: str) -> None:
    """Point Pause/Resume and Stop at ``state``: its label, its icon, and whether it applies.

    Neither item means anything while nothing is being spoken -- resuming silence and stopping
    silence are not actions a user can want -- so both are disabled when the engine is idle.
    """
    paused = state == PAUSED
    label = "Resume" if paused else "Pause"
    pause.setText(label)
    pause.setIcon(QIcon.fromTheme("media-playback-start" if paused else "media-playback-pause"))
    active = state in (PLAYING, PAUSED)
    pause.setEnabled(active)
    stop.setEnabled(active)


def _sync_voice(voice_menu: QMenu | None, voice: str) -> None:
    """Point the exclusive check and the submenu's own title at ``voice``.

    One :class:`QActionGroup` spans every language submenu, so checking the item here is what
    unchecks whichever one was current, and the title is the one place the choice is visible
    without opening anything.
    """
    if voice_menu is None:
        return
    voice_menu.setTitle(f"Voice: {voice_display_name(voice)}")
    # Both the menu's own tooltip and its action's: the action is what the parent menu shows,
    # and Qt only surfaces either where tooltips are switched on (see :func:`build_menu`).
    voice_menu.setToolTip(voice_description(voice))
    voice_menu.menuAction().setToolTip(voice_description(voice))
    for language_menu in voice_menu.findChildren(QMenu):
        for item in language_menu.actions():
            if item.data() is not None:
                item.setChecked(item.data() == voice)


def _sync_speed(speed_menu: QMenu | None, speed: float) -> None:
    """Point the submenu's title and the exclusive check at ``speed``.

    The title carries the value the way the Voice submenu's carries the voice, so the menu
    says what is set without being opened. The slider's range is wider than the menu's
    presets: it can commit 1.37, which no preset names. Leaving every preset unchecked would
    read as "no speed at all", so the item that stands in for a value the menu does not offer
    is shown with that value and left disabled -- the slider is where a custom speed is chosen.
    """
    if speed_menu is None:
        return
    speed_menu.setTitle(f"Speed: {speed:g}×")
    custom = speed_menu.findChild(QAction, "customSpeedAction")
    match = next(
        (
            item
            for item in speed_menu.actions()
            if item.data() is not None and abs(float(item.data()) - speed) < 1e-9
        ),
        None,
    )
    if match is not None:
        if custom is not None:
            custom.setVisible(False)
        match.setChecked(True)
    elif custom is not None:
        custom.setText(f"Custom ({speed:g}×)")
        custom.setVisible(True)
        custom.setChecked(True)


def _populate_voices(
    voice_menu: QMenu,
    config: Config,
    service: SynthService,
    notifier: QSystemTrayIcon | None,
    on_voice_changed: Callable[[str], None] | None,
) -> None:
    """One submenu per language, items named for the speaker, one exclusive group over all.

    Fifty-four flat ids is a list nobody reads; the leading letters already say which language
    a voice speaks, so that becomes the first level. The config still stores the raw id -- the
    name shown here is a display name and nothing else.
    """
    group = QActionGroup(voice_menu)
    group.setExclusive(True)
    # Qt hides menu-item tooltips unless the menu asks for them, and each item's tooltip is the
    # only place a voice's language and gender are spelled out (the group title carries the
    # language alone).
    voice_menu.setToolTipsVisible(True)
    for language, voice_ids in grouped_voices(service.voice_names()):
        language_menu = voice_menu.addMenu(language)
        language_menu.setToolTipsVisible(True)
        for voice_id in voice_ids:
            item = language_menu.addAction(voice_label(voice_id))
            item.setCheckable(True)
            item.setActionGroup(group)
            item.setToolTip(voice_description(voice_id))
            item.setData(voice_id)
            item.triggered.connect(
                lambda _=False, voice_id=voice_id: _choose_voice(
                    config, voice_id, notifier, on_voice_changed
                )
            )


def _choose_voice(
    config: Config,
    voice: str,
    notifier: QSystemTrayIcon | None,
    on_voice_changed: Callable[[str], None] | None,
) -> None:
    """Write the chosen voice, then let the other surface follow it."""
    _set_voice(config, voice, notifier)
    if on_voice_changed is not None:
        on_voice_changed(voice)


def _populate_speeds(
    speed_menu: QMenu,
    config: Config,
    notifier: QSystemTrayIcon | None,
    on_speed_changed: Callable[[float], None] | None,
) -> None:
    """The preset steps, plus the hidden item a custom value takes over (see :func:`_sync_speed`)."""
    group = QActionGroup(speed_menu)
    group.setExclusive(True)
    for speed in SPEEDS:
        item = speed_menu.addAction(f"{speed:g}×")
        item.setCheckable(True)
        item.setActionGroup(group)
        item.setData(speed)
        item.triggered.connect(
            lambda _=False, speed=speed: _choose_speed(
                config, speed, notifier, on_speed_changed
            )
        )
    custom = speed_menu.addAction("")
    custom.setObjectName("customSpeedAction")
    custom.setCheckable(True)
    custom.setActionGroup(group)
    custom.setEnabled(False)  # the slider is where a custom speed is chosen
    custom.setVisible(False)


def _choose_speed(
    config: Config,
    speed: float,
    notifier: QSystemTrayIcon | None,
    on_speed_changed: Callable[[float], None] | None,
) -> None:
    """Write the chosen speed, then let the other surface follow it."""
    _set_speed(config, speed, notifier)
    if on_speed_changed is not None:
        on_speed_changed(speed)


def build_menu(
    config: Config,
    service: SynthService,
    on_quit: Callable[[], None],
    notifier: QSystemTrayIcon | None,
    *,
    on_voice_changed: Callable[[str], None] | None = None,
    on_speed_changed: Callable[[float], None] | None = None,
) -> QMenu:
    """Build the tray menu: speak, pause/resume, stop, voice, speed, autostart, quit.

    ``notifier`` is the icon refusals are shown on, and is required: :class:`Tray`
    passes itself, and a caller with no tray passes ``None`` explicitly rather than
    inheriting a default that would silently swallow every refusal.

    ``on_voice_changed`` and ``on_speed_changed`` are how a choice reaches the *other* surface
    without this module knowing it exists: :class:`Tray` passes its own sync-and-emit hooks,
    and a test with only one surface passes neither.
    """
    menu = QMenu()
    menu.setToolTipsVisible(True)  # the Voice submenu's own tip names the current voice

    speak = menu.addAction("Speak selection")
    speak.setIcon(QIcon.fromTheme("text-speak", QIcon.fromTheme("media-playback-start")))
    speak.triggered.connect(lambda: _speak(config, service, notifier))

    pause = menu.addAction("Pause")
    pause.setObjectName("pauseAction")  # so Tray.set_state can find it to sync
    pause.triggered.connect(lambda: _toggle_pause(service))

    stop = menu.addAction("Stop")
    stop.setObjectName("stopAction")
    stop.setIcon(QIcon.fromTheme("media-playback-stop"))
    stop.triggered.connect(service.stop)

    menu.addSeparator()  # playback above, settings below: two groups, not one list

    voice_menu = menu.addMenu("Voice")
    voice_menu.setObjectName("voiceMenu")
    _populate_voices(voice_menu, config, service, notifier, on_voice_changed)

    speed_menu = menu.addMenu("Speed")
    speed_menu.setObjectName("speedMenu")
    _populate_speeds(speed_menu, config, notifier, on_speed_changed)

    autostart = menu.addAction("Start at login")
    autostart.setCheckable(True)
    # The key is the source of truth, so the check reflects the intent even when the file could
    # not be written; the toggle writes the key and then brings the file into line with it.
    autostart.setChecked(config.general.autostart)
    autostart.triggered.connect(
        lambda checked=False: _set_autostart(config, checked, notifier)
    )

    menu.addSeparator()
    quit_item = menu.addAction("Quit")
    quit_item.setIcon(QIcon.fromTheme("application-exit"))
    quit_item.triggered.connect(lambda: on_quit())

    # The initial state is the engine's, not a guess: the overlay may have been paused before
    # this menu was built, and the voice or speed may have been set from a previous session.
    state = str(service.status().get("state", IDLE)).upper()
    _sync_playback(pause, stop, state)
    _sync_voice(voice_menu, config.speech.voice)
    _sync_speed(speed_menu, config.speech.speed)

    return menu


class Tray(QSystemTrayIcon):
    """The tray icon, its state and the menu built by :func:`build_menu`.

    The icon is the app's own, and the only state cue is the tooltip: a state that needs an
    animated icon is a state a tooltip cannot describe, and this one can.
    """

    voice_changed = pyqtSignal(str)
    """The voice just chosen, so the overlay can show it without polling the config file."""

    speed_changed = pyqtSignal(float)
    """The speed just chosen, so the overlay's slider can follow the menu."""

    def __init__(
        self, service: SynthService, config: Config, on_quit: Callable[[], None]
    ) -> None:
        super().__init__()
        self._config = config
        self._menu = build_menu(
            config,
            service,
            on_quit,
            notifier=self,
            on_voice_changed=self._voice_picked,
            on_speed_changed=self._speed_picked,
        )
        self.setIcon(app_icon())
        self.setContextMenu(self._menu)
        self._pause = self._menu.findChild(QAction, "pauseAction")
        self._stop = self._menu.findChild(QAction, "stopAction")
        self.set_state(str(service.status().get("state", IDLE)))

    def set_state(self, state: str) -> None:
        """Reflect the engine's state in the tooltip and in what the menu offers."""
        state = state.upper()
        self.setToolTip(_tooltip(state, self._config.speech.voice))
        if self._pause is not None and self._stop is not None:
            _sync_playback(self._pause, self._stop, state)

    def set_voice(self, voice: str) -> None:
        """Reflect ``voice`` in the menu's check and in the submenu's title."""
        _sync_voice(self._menu.findChild(QMenu, "voiceMenu"), voice)

    def set_speed(self, speed: float) -> None:
        """Reflect ``speed`` in the menu, including a value no preset matches."""
        _sync_speed(self._menu.findChild(QMenu, "speedMenu"), speed)

    def _voice_picked(self, voice: str) -> None:
        self.set_voice(voice)
        self.voice_changed.emit(voice)

    def _speed_picked(self, speed: float) -> None:
        self.set_speed(speed)
        self.speed_changed.emit(speed)


def _tooltip(state: str, voice: str) -> str:
    """What the tray says on hover: the state, and which voice is speaking it."""
    if state == PLAYING:
        return f"Utter — Speaking ({voice_display_name(voice)})"
    if state == PAUSED:
        return "Utter — Paused"
    return "Utter — Ready"
