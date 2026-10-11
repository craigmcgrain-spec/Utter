"""The floating controller drives the engine without holding any playback state.

A reader who only checks the buttons would miss the speed rule: the engine applies speed
from the next synthesised unit, so a *drag* must commit exactly once, on release, while a
change that is not a drag -- a wheel notch, an arrow key -- must commit at once, or the
user is left looking at a speed the engine never received. They would also miss that the
widget must not fight a compositor that ignores client-side positioning, since ``move()``
is a no-op under Wayland.
"""

from utter.ui.overlay import Overlay


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


def test_toggle_reflects_the_service_state(qapp, fake_service, config):
    # The overlay keeps no playback state of its own: the toggle must follow status().
    fake_service.status = lambda: {"paused": True}
    overlay = Overlay(fake_service, config)
    overlay.show_for_state("PAUSED")
    assert "resume" in overlay.toggle_button.text().lower()
    fake_service.status = lambda: {"paused": False}
    overlay.show_for_state("PLAYING")
    assert "pause" in overlay.toggle_button.text().lower()


def test_speed_slider_range_is_spec_bounds(qapp, fake_service, config):
    overlay = Overlay(fake_service, config)
    assert (overlay.speed_slider.minimum(), overlay.speed_slider.maximum()) == (75, 200)


def test_set_speed_moves_the_slider(qapp, fake_service, config):
    overlay = Overlay(fake_service, config)
    overlay.set_speed(1.5)
    assert overlay.speed_slider.value() == 150


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


def test_voice_label_shows_the_configured_voice(qapp, fake_service, config):
    config.speech.voice = "bf_emma"
    overlay = Overlay(fake_service, config)
    assert overlay.voice_label.text() == "bf_emma"
