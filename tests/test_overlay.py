"""The floating controller drives the engine without holding any playback state.

A reader who only checks the buttons would miss the two easy things to get wrong: the
slider must commit its speed exactly once, on release, because the engine applies speed
from the next synthesised unit and a drag fires dozens of values; and the widget must
not fight a compositor that ignores client-side positioning, since ``move()`` is a no-op
under Wayland.
"""

from utter.ui.overlay import Overlay


def test_play_pause_toggles_the_service(qapp, fake_service, config):
    overlay = Overlay(fake_service, config)
    overlay.toggle_button.click()
    assert fake_service.calls == ["pause"]
    overlay.toggle_button.click()
    assert fake_service.calls == ["pause", "resume"]


def test_stop_button_stops(qapp, fake_service, config):
    # The overlay must be bound: PyQt owns a parentless widget from Python, so a bare
    # ``Overlay(...).stop_button`` would dereference a temporary whose C++ widget CPython
    # destroys the moment the expression drops it, raising on the click.
    overlay = Overlay(fake_service, config)
    overlay.stop_button.click()
    assert fake_service.calls == ["stop"]


def test_speed_emits_only_on_release(qapp, fake_service, config):
    overlay = Overlay(fake_service, config)
    seen = []
    overlay.speed_committed.connect(seen.append)
    overlay.speed_slider.setValue(150)          # mid-drag
    assert seen == []
    overlay.speed_slider.sliderReleased.emit()  # release
    assert seen == [1.5]


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
    assert Overlay(fake_service, config).voice_label.text() == "bf_emma"
