"""The floating controller: the small always-on-top widget shown while speech plays.

It owns no playback state. The buttons call the service, and the play/pause label and
check come from ``service.status()["paused"]`` on every state change, so the widget
cannot drift from the engine. A speed *drag* commits exactly once, on release, because the
engine applies speed from the next synthesised unit and a drag would otherwise fire dozens
of changes; any change that is not a drag -- a wheel notch, an arrow key -- commits at once,
so the shown value never lags what the engine received. The remembered position is restored
only on X11, because a Wayland compositor ignores client-side positioning and placing the
window is its decision, not ours.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from PyQt6.QtCore import QSignalBlocker, Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSlider, QWidget

from ..config import SPEED_MAX, SPEED_MIN, Config

if TYPE_CHECKING:
    # Annotation only: importing the engine at runtime would drag numpy and the audio
    # stack into every module that imports the overlay, which needs only the interface.
    from ..tts.engine import SynthService

_SLIDER_MIN = round(SPEED_MIN * 100)
_SLIDER_MAX = round(SPEED_MAX * 100)
"""The slider is integer percent; the engine wants 0.75-2.0, so the two are 100x apart."""

_SHOWING_STATES = ("PLAYING", "PAUSED")
"""The states the overlay is wanted in; ``IDLE`` hides it."""


class Overlay(QWidget):
    """Play/pause, stop, speed and the voice name for the currently playing speech.

    The widget is deliberately passive: every button delegates to the service, and the
    only state it reflects is what :meth:`show_for_state` last told it plus the service's
    own ``status()``.
    """

    speed_committed = pyqtSignal(float)
    """The settled speed: once per drag (on release), or at once for a non-drag change."""

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

        self.toggle_button = QPushButton("Pause")
        self.toggle_button.setCheckable(True)
        self.toggle_button.clicked.connect(self._toggle)

        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self._service.stop)

        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setMinimum(_SLIDER_MIN)
        self.speed_slider.setMaximum(_SLIDER_MAX)
        self.speed_slider.setValue(round(config.speech.speed * 100))
        self.speed_slider.setToolTip("Speed applies from the next sentence.")
        self.speed_slider.sliderReleased.connect(self._commit_speed)
        self.speed_slider.valueChanged.connect(self._on_value_changed)

        self.voice_label = QLabel(config.speech.voice)

        layout = QHBoxLayout(self)
        layout.addWidget(self.toggle_button)
        layout.addWidget(self.stop_button)
        layout.addWidget(self.speed_slider)
        layout.addWidget(self.voice_label)

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
        if not self.speed_slider.isSliderDown():
            self._commit_speed()

    def _commit_speed(self) -> None:
        """Report the settled speed: once per drag release, or at once off a drag."""
        self.speed_committed.emit(self.speed_slider.value() / 100.0)

    def _sync_toggle(self) -> None:
        """Take the play/pause label and check from the service, the owner of that state."""
        paused = bool(self._service.status().get("paused", False))
        self.toggle_button.setChecked(paused)
        self.toggle_button.setText("Resume" if paused else "Pause")

    def set_speed(self, speed: float) -> None:
        """Move the slider to ``speed`` without committing it."""
        with QSignalBlocker(self.speed_slider):
            self.speed_slider.setValue(round(speed * 100))

    def show_for_state(self, state: str) -> None:
        """Show while speech plays or pauses, hide when idle, honouring ``show_overlay``."""
        speaking = state.upper() in _SHOWING_STATES
        if not (self._config.ui.show_overlay and speaking):
            if self.isVisible():
                self.hide()
            return
        self._sync_toggle()
        if not self.isVisible():
            self.show()

    def _restore_position(self) -> None:
        """Restore the remembered position, but only where the session lets us place it.

        A Wayland compositor ignores client-side positioning, so ``move()`` is a no-op
        there and the compositor decides where the widget lands; the tray menu carries
        the same controls. A move that does not take effect is not an error.
        """
        if os.environ.get("XDG_SESSION_TYPE", "").lower() != "x11":
            return
        x, y = self._config.ui.overlay_pos
        self.move(x, y)
