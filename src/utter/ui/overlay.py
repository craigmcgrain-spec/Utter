"""The floating controller: the small always-on-top widget shown while speech plays.

It owns no playback state. The buttons call the service, and the play/pause icon and tooltip
come from ``service.status()["paused"]`` on every state change, so the widget cannot drift from
the engine. A speed *drag* commits exactly once, on release, because the engine applies speed
from the next synthesised unit and a drag would otherwise fire dozens of changes; any change
that is not a drag -- a wheel notch, an arrow key -- commits at once, so the shown value never
lags what the engine received. The readout beside the slider is display only: it follows every
slider value, drag or not, while committing keeps the rule above.

Nothing here hard-codes a colour. The buttons ask the desktop's icon theme for their glyphs and,
where it has none, paint the same glyphs in the palette's own colour; the panel border, the grip
dots and the readouts all come from the palette too, so the widget reads as native in both a
light and a dark session. The remembered position is restored only on X11, because a
Wayland compositor ignores client-side positioning and placing the window is its decision, not
ours; for the same reason a move is only persisted on X11, where the coordinates are ours.

Keyboard focus is the compositor's to give, and on Wayland it is given whether the widget asks
for it or not (see the window flags in :meth:`Overlay.__init__`); on Plasma the installer's KWin
rule withholds it. Where the widget does get the keyboard -- X11, or a compositor that grants it
-- it treats it as a real path: Space and Escape, and a tab cycle whose stops each show the
style's own focus cue, including the panel itself.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from PyQt6.QtCore import QEvent, QPoint, QPointF, QSignalBlocker, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QIcon,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPalette,
    QPixmap,
    QPolygonF,
)
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSlider,
    QStyle,
    QStyleOptionFocusRect,
    QToolButton,
    QWidget,
)

from ..config import SPEED_MAX, SPEED_MIN, Config
from .voices import voice_description, voice_display_name

if TYPE_CHECKING:
    # Annotation only: importing the engine at runtime would drag numpy and the audio
    # stack into every module that imports the overlay, which needs only the interface.
    from ..tts.engine import SynthService

_SLIDER_MIN = round(SPEED_MIN * 100)
_SLIDER_MAX = round(SPEED_MAX * 100)
"""The slider is integer percent; the engine wants 0.75-2.0, so the two are 100x apart."""

_SHOWING_STATES = ("PLAYING", "PAUSED")
"""The states the overlay is wanted in; ``IDLE`` hides it."""

_POSITION_SETTLE_MS = 250
"""How long the window must sit still before a move counts as finished.

``startSystemMove`` hands the drag to the compositor and reports nothing back: there is no
"the move ended" signal, and under a compositor that grabs the pointer the button release may
not even reach this widget. The end of the move is therefore inferred from the position going
quiet. Persisting on every move event instead would rewrite ``config.toml`` -- through a
temporary file and ``os.replace`` -- dozens of times per drag.
"""

_GLYPH_PX = 48
"""The fallback glyphs are painted at this square and scaled down by the button.

Painting at the size the button asks for would give a jagged edge at 16 px; drawing large and
letting the style scale is what keeps the fallback as crisp as a themed icon.
"""


def _painted_icon(name: str, palette: QPalette) -> QIcon:
    """The fallback glyph for ``name``, painted in the palette's own button-text colour.

    Qt's bundled standard pixmaps for these three are drawn in black whatever the palette, so
    on a dark desktop they are a black glyph on a dark button. Painting them from the palette
    keeps the fallback legible in both schemes, which is the whole point of the fallback.
    """
    pixmap = QPixmap(_GLYPH_PX, _GLYPH_PX)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(palette.color(QPalette.ColorRole.ButtonText))
    if name == "media-playback-pause":
        painter.drawRect(13, 12, 8, 24)
        painter.drawRect(27, 12, 8, 24)
    elif name == "media-playback-start":
        painter.drawPolygon(
            QPolygonF([QPointF(16, 11), QPointF(38, 24), QPointF(16, 37)])
        )
    else:  # media-playback-stop
        painter.drawRect(14, 14, 20, 20)
    painter.end()
    return QIcon(pixmap)


def _button_icon(name: str, palette: QPalette) -> QIcon:
    """The desktop's icon for ``name``, or the palette-painted glyph where there is none.

    A named icon is what makes the button look native: it picks up whichever icon theme the
    session chose, so the widget follows a light or a dark desktop without a stylesheet of its
    own. A theme with no such icon -- or the offscreen platform, which has no theme at all --
    would otherwise draw an empty button, so the fallback stands in.
    """
    icon = QIcon.fromTheme(name)
    return icon if not icon.isNull() else _painted_icon(name, palette)


def _on_x11() -> bool:
    """Whether this session lets the client place its own top-level windows.

    ``XDG_SESSION_TYPE`` is what the rest of the app's positioning decisions read; anything
    that is not exactly ``x11`` -- ``wayland``, an empty variable, a compositor that reports
    neither -- is treated as "the compositor decides", which is the safe reading.
    """
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "x11"


class _DragGrip(QWidget):
    """The grip at the leading edge that moves the window.

    Painted rather than iconographic: six dots in the palette's own text colour at low alpha,
    so the grip is legible on a light and on a dark panel where a bundled image would have to
    be re-coloured for both. The press is handed to the compositor through
    ``QWindow.startSystemMove()``, which is the only thing that moves a window on Wayland and
    works on X11 as well.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setFixedWidth(10)
        self.setToolTip("Drag to move")

    def paintEvent(self, event: object) -> None:  # noqa: N802 - Qt's spelling
        painter = QPainter(self)
        colour = QColor(self.palette().color(QPalette.ColorRole.WindowText))
        colour.setAlphaF(0.45)
        painter.setBrush(colour)
        painter.setPen(Qt.PenStyle.NoPen)
        centre_x = self.width() / 2
        centre_y = self.height() / 2
        for column in (-2.5, 2.5):
            for row in (-5.0, 0.0, 5.0):
                painter.drawEllipse(QPointF(centre_x + column, centre_y + row), 1.5, 1.5)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt's spelling
        """Hand the drag to the compositor; a press with nothing to move falls through."""
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return
        super().mousePressEvent(event)


class Overlay(QFrame):
    """Play/pause, stop, speed and the voice name for the currently playing speech.

    The widget is deliberately passive: every button delegates to the service, and the only
    state it reflects is what :meth:`show_for_state` last told it plus the service's own
    ``status()``.
    """

    speed_committed = pyqtSignal(float)
    """The settled speed: once per drag (on release), or at once for a non-drag change."""

    moved = pyqtSignal(int, int)
    """The window's settled position after a drag, on X11 only -- there it is ours to keep."""

    def __init__(
        self, service: SynthService, config: Config, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._config = config

        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        # A controller floating over the desktop must not take the keyboard away from what the
        # user is reading, which is exactly what showing an activating Tool window does.
        #
        # ``WA_ShowWithoutActivating`` states that intent, and Qt-only additions were measured
        # against a KWin script that logs every activation (KDE Plasma 6 Wayland, focus stealing
        # prevention "Low"). None of them stop it: with this attribute, with
        # ``WindowDoesNotAcceptFocus`` as well, with neither, and even for a bare ``QWidget``
        # that asks for nothing at all, KWin activates the newly mapped window every time --
        # including a second mapping -- and only hands the keyboard back when the window hides.
        # The compositor is deciding, so the client-side flag (which would also cost the
        # click-to-focus Space and Escape rely on where focus is granted) was not kept. On
        # Plasma, install.sh adds a KWin window rule for app-id ``utter`` that forces "accept
        # focus: no" and skips the taskbar -- measured to stop both. See the README.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        # What a task manager shows for this window where nothing keeps it off one (Wayland
        # outside Plasma's installed rule): it should at least say whose it is.
        self.setWindowTitle("Utter")

        self.grip = _DragGrip(self)

        # Flat until hovered, as a desktop media controller's buttons are; a bevel round each
        # glyph made the two buttons heavier than the slider and the readouts they sit with.
        # Auto-raised rather than plain: measured here, that is the shape the style gives a
        # keyboard focus cue to, and a plain tool button stays silent.
        self.toggle_button = QToolButton(self)
        self.toggle_button.setAutoRaise(True)
        self.toggle_button.setCheckable(True)
        self.toggle_button.clicked.connect(self._toggle)

        self.stop_button = QToolButton(self)
        self.stop_button.setAutoRaise(True)
        self.stop_button.setIcon(_button_icon("media-playback-stop", self.palette()))
        self.stop_button.setToolTip("Stop")
        self.stop_button.setAccessibleName("Stop")
        self.stop_button.clicked.connect(self._service.stop)

        self.speed_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.speed_slider.setMinimum(_SLIDER_MIN)
        self.speed_slider.setMaximum(_SLIDER_MAX)
        self.speed_slider.setMinimumWidth(110)
        self.speed_slider.setValue(round(config.speech.speed * 100))
        self.speed_slider.setToolTip("Speed applies from the next sentence.")
        self.speed_slider.sliderReleased.connect(self._commit_speed)
        self.speed_slider.valueChanged.connect(self._on_value_changed)

        self.speed_label = QLabel(self)
        # Leading-aligned so the value sits against the slider it reads out; the fixed width
        # (below) absorbs "1×" versus "1.25×" on the far side instead of opening a gap here.
        self.speed_label.setAlignment(
            Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignVCenter
        )

        self.voice_label = QLabel(self)
        # No reserved width: measured at 1x, reserving the longest name in the pack (``Gongitsune``)
        # left a 38px hole after a five-letter name and another 28px between it and the readout,
        # and right-aligning inside that reservation only moved the hole to the middle of the
        # panel. Sized to its text, the name sits a group separation away from the readout on
        # every voice; the panel's width then follows the name, which is a change only a voice
        # change can make, and that is a choice taken from the tray menu.
        metrics = self.fontMetrics()
        # The readout keeps its reservation: a drag rewrites the text continuously ("0.75×" to
        # "1.25×"), and a panel that tracked that would resize under the pointer holding the
        # slider.
        self.speed_label.setMinimumWidth(metrics.horizontalAdvance("0.75×") + 2)

        self._settle_timer = QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.setInterval(_POSITION_SETTLE_MS)
        self._settle_timer.timeout.connect(self._report_position)

        self._last_position = QPoint(*config.ui.overlay_pos)
        """The position already known to the config file, so a restore is not written back."""

        # Symmetric margins: the name ends as far from the frame as the grip starts from it.
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(6)
        layout.addWidget(self.grip)
        layout.addWidget(self.toggle_button)
        layout.addWidget(self.stop_button)
        # The buttons, then the setting they act on: the slider and the readout that belongs to
        # it are one group at 6px, and the voice name -- status, not a control -- stands a
        # group separation away from them.
        layout.addSpacing(2)
        layout.addWidget(self.speed_slider)
        layout.addWidget(self.speed_label)
        layout.addSpacing(10)
        layout.addWidget(self.voice_label)

        # A fresh widget is not paused: the service has not been asked to pause and the first
        # thing that shows this window is a state change, which syncs from the service anyway.
        self._apply_toggle(paused=False)
        self.set_voice(config.speech.voice)
        self._update_speed_readout()

        self.hide()
        self._restore_position()

    def _toggle(self) -> None:
        """Pause when the button is now checked, resume when it is not.

        The check lives on the button, not in a flag here; the authoritative paused
        state is the service's and is read back in :meth:`_sync_toggle`.
        """
        if self.toggle_button.isChecked():
            self._service.pause()
        else:
            self._service.resume()

    def _on_value_changed(self) -> None:
        """Commit at once a change that is not a drag: a wheel notch, an arrow key, a click.

        A drag's intermediate values are deliberately skipped here and committed once by
        :meth:`_commit_speed` on release.
        """
        self._update_speed_readout()
        if not self.speed_slider.isSliderDown():
            self._commit_speed()

    def _commit_speed(self) -> None:
        """Report the settled speed: once per drag release, or at once off a drag."""
        self.speed_committed.emit(self.speed_slider.value() / 100.0)

    def _update_speed_readout(self) -> None:
        """Show the slider's own value, so a drag reads out while it is still happening."""
        self.speed_label.setText(f"{self.speed_slider.value() / 100:g}×")

    def _sync_toggle(self) -> None:
        """Take the paused state from the service, the owner of it, and show it."""
        self._apply_toggle(paused=bool(self._service.status().get("paused", False)))

    def _apply_toggle(self, *, paused: bool) -> None:
        """Show ``paused``: the check, the glyph and the label a keyboard user hears.

        The icon swapping to *play* is what makes a paused overlay obvious -- a frameless Tool
        window has no title bar to carry the state, and a check mark is not drawn on a
        QToolButton -- so the label and the glyph have to move together.
        """
        self.toggle_button.setChecked(paused)
        label = "Resume" if paused else "Pause"
        self.toggle_button.setIcon(
            _button_icon(
                "media-playback-start" if paused else "media-playback-pause", self.palette()
            )
        )
        self.toggle_button.setToolTip(label)
        self.toggle_button.setAccessibleName(label)

    def set_voice(self, voice: str) -> None:
        """Show ``voice`` as a name, with the language and gender left to the tooltip."""
        self.voice_label.setText(voice_display_name(voice))
        self.voice_label.setToolTip(voice_description(voice))

    def set_speed(self, speed: float) -> None:
        """Move the slider to ``speed`` without committing it, e.g. after a tray choice."""
        with QSignalBlocker(self.speed_slider):
            self.speed_slider.setValue(round(speed * 100))
        self._update_speed_readout()

    def show_for_state(self, state: str) -> None:
        """Show while speech plays or pauses, hide when idle, honouring ``show_overlay``."""
        speaking = state.upper() in _SHOWING_STATES
        if not (self._config.ui.show_overlay and speaking):
            if self.isVisible():
                self.hide()
            return
        self._sync_toggle()
        # The voice may have been changed from the tray while the overlay was hidden, and the
        # config is where that change landed: read it rather than trust the label's last value.
        self.set_voice(self._config.speech.voice)
        if not self.isVisible():
            self.show()

    def paintEvent(self, event: object) -> None:  # noqa: N802 - Qt's spelling
        """Draw the panel, then the focus cue the style has no frame for.

        The tab cycle ends on the panel itself -- that stop is where Space and Escape belong to
        the widget rather than to a button -- and a plain frame paints no focus rectangle, so
        the cue disappeared on the last stop of the cycle. The primitive drawn here is the
        style's own, so it behaves like the desktop's: nothing for focus that arrived with the
        pointer, and a rectangle on every stop the keyboard can reach.
        """
        super().paintEvent(event)
        if not self.hasFocus():
            return
        option = QStyleOptionFocusRect()
        option.initFrom(self)
        option.rect = self.rect().adjusted(3, 3, -3, -3)
        painter = QPainter(self)
        self.style().drawPrimitive(
            QStyle.PrimitiveElement.PE_FrameFocusRect, option, painter, self
        )

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt's spelling
        """Space pauses or resumes, Escape stops, while the overlay has the keyboard.

        The Tool window has no menu and no other keyboard surface, so the two actions the
        buttons carry are reachable without a pointer too. This is not a global grab: it only
        applies once the widget itself has focus.
        """
        if event.key() == Qt.Key.Key_Space:
            self.toggle_button.click()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape:
            self._service.stop()
            event.accept()
            return
        super().keyPressEvent(event)

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt's spelling
        """Redraw the fallback glyphs when the desktop switches between light and dark.

        A session with an icon theme needs none of this -- its glyphs come back from
        :func:`QIcon.fromTheme` already the right colour -- but the painted fallback keeps the
        colour it was drawn with, and this widget lives for as long as the tray does.
        """
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange and getattr(self, "stop_button", None):
            self.stop_button.setIcon(_button_icon("media-playback-stop", self.palette()))
            self._apply_toggle(paused=self.toggle_button.isChecked())

    def moveEvent(self, event: object) -> None:  # noqa: N802 - Qt's spelling
        """Restart the settle timer on every move; only the last position is worth keeping.

        Nothing is started on a session that places the window itself: the coordinates a
        Wayland compositor hands out are not the client's to remember, and the tray menu
        carries every control the overlay does.
        """
        super().moveEvent(event)
        if _on_x11():
            self._settle_timer.start()

    def _report_position(self) -> None:
        """Hand the settled position on, unless it is the one already in the file.

        The restore in :meth:`_restore_position` moves the window to the config's own value,
        and that move must not come straight back as a save: nothing about the position has
        changed, and a startup that rewrites ``config.toml`` would look like the app had moved
        the window itself.
        """
        position = self.pos()
        if position == self._last_position:
            return
        self._last_position = position
        self.moved.emit(position.x(), position.y())

    def _restore_position(self) -> None:
        """Restore the remembered position, but only where the session lets us place it.

        A Wayland compositor ignores client-side positioning, so ``move()`` is a no-op
        there and the compositor decides where the widget lands; the tray menu carries
        the same controls. A move that does not take effect is not an error.
        """
        if not _on_x11():
            return
        x, y = self._config.ui.overlay_pos
        self.move(x, y)
