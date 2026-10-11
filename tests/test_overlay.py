"""The floating controller drives the engine without holding any playback state.

A reader who only checks the buttons would miss the speed rule: the engine applies speed
from the next synthesised unit, so a *drag* must commit exactly once, on release, while a
change that is not a drag -- a wheel notch, an arrow key -- must commit at once, or the
user is left looking at a speed the engine never received. They would also miss that the
widget must not fight a compositor that ignores client-side positioning, since ``move()``
is a no-op under Wayland and only X11 has a position worth remembering.
"""

import time

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtTest import QTest

import utter.ui.overlay as overlay_module
from utter.ui.overlay import Overlay


def _pump_until(qapp, predicate, timeout=2.0):
    """Pump the event loop until ``predicate`` holds, or the timeout expires; report which."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    qapp.processEvents()
    return predicate()


def _drain(qapp, seconds=0.15):
    """Pump the event loop for a fixed span, so a timer that should not fire gets its chance."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)


def test_play_pause_toggles_the_service(qapp, fake_service, config):
    overlay = Overlay(fake_service, config)
    overlay.toggle_button.click()
    assert fake_service.calls == ["pause"]
    overlay.toggle_button.click()
    assert fake_service.calls == ["pause", "resume"]


def test_stop_button_stops(qapp, fake_service, config):
    # The overlay is bound to a name: a parentless PyQt widget created as a temporary is
    # destroyed as soon as CPython drops the reference, and the button click then raises.
    overlay = Overlay(fake_service, config)
    overlay.stop_button.click()
    assert fake_service.calls == ["stop"]


def test_speed_commits_once_per_drag(qapp, fake_service, config):
    overlay = Overlay(fake_service, config)
    seen = []
    overlay.speed_committed.connect(seen.append)
    overlay.speed_slider.setSliderDown(True)      # a real drag begins
    overlay.speed_slider.setValue(120)
    overlay.speed_slider.setValue(150)            # mid-drag changes must not be emitted
    assert seen == []
    overlay.speed_slider.sliderReleased.emit()    # release
    assert seen == [1.5]


def test_speed_commits_immediately_when_not_dragging(qapp, fake_service, config):
    # A wheel notch or an arrow key is not a drag. Leaving it uncommitted shows the user a
    # speed the engine never received, so any value change outside a drag commits at once.
    overlay = Overlay(fake_service, config)
    seen = []
    overlay.speed_committed.connect(seen.append)
    overlay.speed_slider.setValue(150)
    assert seen == [1.5]


def test_the_speed_readout_follows_a_drag_without_committing_it(qapp, fake_service, config):
    # The readout is display only: it must update while the drag is still happening, which is
    # the whole reason it exists, and it must not turn a drag into dozens of commits.
    overlay = Overlay(fake_service, config)
    seen = []
    overlay.speed_committed.connect(seen.append)
    overlay.speed_slider.setSliderDown(True)
    overlay.speed_slider.setValue(133)
    assert overlay.speed_label.text() == "1.33×"
    overlay.speed_slider.setValue(75)
    assert overlay.speed_label.text() == "0.75×"
    assert seen == [], "a drag still commits only on release"


def test_set_speed_moves_the_slider_and_the_readout(qapp, fake_service, config):
    # The tray's Speed menu is the other surface that sets this; moving the slider without
    # updating the readout would leave the widget showing a speed it is not at.
    overlay = Overlay(fake_service, config)
    overlay.set_speed(1.5)
    assert overlay.speed_slider.value() == 150
    assert overlay.speed_label.text() == "1.5×"


def test_the_paused_state_is_visible_in_the_button(qapp, fake_service, config):
    # The overlay keeps no playback state of its own: the toggle follows status(), and the
    # paused state has to be obvious without reading a check mark that a frameless tool
    # window never draws.
    fake_service.status = lambda: {"paused": True}
    overlay = Overlay(fake_service, config)
    overlay.show_for_state("PAUSED")
    assert overlay.toggle_button.isChecked()
    assert overlay.toggle_button.toolTip() == "Resume"
    assert overlay.toggle_button.accessibleName() == "Resume"

    fake_service.status = lambda: {"paused": False}
    overlay.show_for_state("PLAYING")
    assert not overlay.toggle_button.isChecked()
    assert overlay.toggle_button.toolTip() == "Pause"
    assert overlay.toggle_button.accessibleName() == "Pause"


def test_every_button_is_labelled_and_draws_an_icon(qapp, fake_service, config):
    # The buttons have no text, so their tooltip and accessible name *are* the label: without
    # them the widget is three unlabelled glyphs to anyone not looking straight at it. The
    # icons must also render on a platform whose icon theme is empty, which is what the
    # standard-pixmap fallback is for -- and this suite runs on exactly such a platform.
    overlay = Overlay(fake_service, config)
    for button in (overlay.toggle_button, overlay.stop_button):
        assert not button.icon().isNull(), "a theme-less session must still get a glyph"
        assert button.toolTip(), "an unlabelled button needs a tooltip"
        assert button.accessibleName(), "an unlabelled button needs an accessible name"


def test_speed_slider_range_is_spec_bounds(qapp, fake_service, config):
    overlay = Overlay(fake_service, config)
    assert (overlay.speed_slider.minimum(), overlay.speed_slider.maximum()) == (75, 200)


def test_show_for_state_shows_while_speaking_and_hides_when_idle(qapp, fake_service, config):
    overlay = Overlay(fake_service, config)
    overlay.show_for_state("PLAYING")
    assert overlay.isVisible()
    overlay.show_for_state("PAUSED")
    assert overlay.isVisible()
    overlay.show_for_state("IDLE")
    assert not overlay.isVisible()


def test_show_for_state_respects_the_overlay_setting(qapp, fake_service, config):
    config.ui.show_overlay = False
    overlay = Overlay(fake_service, config)
    overlay.show_for_state("PLAYING")
    assert not overlay.isVisible()


def test_showing_the_overlay_does_not_take_the_keyboard(qapp, fake_service, config):
    # A Tool window that activated itself on show would pull focus off whatever the user is
    # reading, which is the one thing a controller floating over the desktop must not do.
    overlay = Overlay(fake_service, config)
    assert overlay.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)


def test_the_voice_label_reports_a_choice_made_elsewhere(qapp, fake_service, config):
    # The label is set from the model in __init__ and has to follow every later change too:
    # the tray writes the config, and this is what makes the overlay agree with it.
    overlay = Overlay(fake_service, config)
    overlay.set_voice("bf_emma")
    assert overlay.voice_label.text() == "Emma"
    assert "British English" in overlay.voice_label.toolTip()


def test_show_for_state_refreshes_the_voice_from_the_config(qapp, fake_service, config):
    # What the tray writes is the config, so re-reading it on show is what guarantees the
    # label is current even when no signal reached the overlay while it was hidden.
    overlay = Overlay(fake_service, config)
    config.speech.voice = "am_michael"
    overlay.show_for_state("PLAYING")
    assert overlay.voice_label.text() == "Michael"


def test_space_pauses_and_escape_stops(qapp, fake_service, config):
    # The overlay is a frameless Tool window, so its buttons are the pointer's only way in;
    # the keyboard needs the two actions too, without any global grab.
    overlay = Overlay(fake_service, config)
    overlay.setFocus()
    QTest.keyClick(overlay, Qt.Key.Key_Space)
    assert fake_service.calls == ["pause"]
    QTest.keyClick(overlay, Qt.Key.Key_Escape)
    assert fake_service.calls == ["pause", "stop"]


def test_the_grip_hands_the_drag_to_the_compositor(qapp, fake_service, config, monkeypatch):
    # startSystemMove is the only thing that moves a window under Wayland and it works on X11
    # too; a hand-rolled mouse-move would work on exactly one of them.
    overlay = Overlay(fake_service, config)
    started = []

    class Handle:
        def startSystemMove(self):
            started.append(True)
            return True

    monkeypatch.setattr(Overlay, "windowHandle", lambda self: Handle())
    press = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(4, 4),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    overlay.grip.mousePressEvent(press)
    assert started == [True], "the grip must ask the compositor to move the window"


def test_a_settled_move_is_reported_on_x11(qapp, fake_service, config, monkeypatch):
    # Nothing saved the position before, so the remembered one was always the default: the
    # overlay has to hand its settled position on, on the only session where it is ours.
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setattr(overlay_module, "_POSITION_SETTLE_MS", 20)
    overlay = Overlay(fake_service, config)
    seen = []
    overlay.moved.connect(lambda x, y: seen.append((x, y)))
    overlay.show_for_state("PLAYING")  # a hidden widget is not where a drag can move it

    overlay.move(40, 60)
    assert _pump_until(qapp, lambda: bool(seen)), "a finished move must be reported"
    assert seen == [(40, 60)]


def test_the_restored_position_is_not_written_back(qapp, fake_service, config, monkeypatch):
    # The restore moves the window to the value the file already holds; saving that back would
    # rewrite config.toml on every startup for no change at all.
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setattr(overlay_module, "_POSITION_SETTLE_MS", 20)
    config.ui.overlay_pos = (12, 34)
    overlay = Overlay(fake_service, config)
    seen = []
    overlay.moved.connect(lambda x, y: seen.append((x, y)))

    overlay.show_for_state("PLAYING")  # the restore's own move only lands once it is shown
    _drain(qapp)
    assert seen == []


def test_a_move_is_not_reported_where_the_compositor_places_the_window(
    qapp, fake_service, config, monkeypatch
):
    # Wayland hands out the coordinates; remembering them would be the client claiming a
    # position it never set, and the tray menu carries every control the overlay does.
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setattr(overlay_module, "_POSITION_SETTLE_MS", 20)
    overlay = Overlay(fake_service, config)
    seen = []
    overlay.moved.connect(lambda x, y: seen.append((x, y)))
    overlay.show_for_state("PLAYING")

    overlay.move(40, 60)
    _drain(qapp)
    assert seen == []
