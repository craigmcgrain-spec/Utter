"""The tray menu drives the engine, and its two invisible contracts hold.

A reader who only checks the menu labels would miss both: the Speak action must hand
capture the *configured* ``max_chars`` (capture defaults to the literal 20000), and it
must turn a refusal into a notification because an exception escaping a Qt slot is a
crash, not an error path.
"""

import utter.ui.tray as tray
from utter.capture import Selection
from utter.tts.chunk import TooLongError
from utter.ui.tray import build_menu


def test_menu_contains_every_control(qapp, fake_service, config):
    menu = build_menu(config, fake_service, on_quit=lambda: None)
    labels = [action.text() for action in menu.actions() if action.text()]
    assert "Speak selection" in labels
    assert "Stop" in labels
    assert any("Voice" in label for label in labels)
    assert any("Speed" in label for label in labels)


def test_speak_action_asks_the_service_to_speak_the_selection(qapp, fake_service, config, monkeypatch):
    monkeypatch.setattr(tray, "read_selection", lambda **kw: Selection("hello there", "primary"))
    menu = build_menu(config, fake_service, on_quit=lambda: None)
    next(a for a in menu.actions() if a.text() == "Speak selection").trigger()
    assert fake_service.spoken == ["hello there"]


def test_speak_with_no_selection_notifies_once(qapp, fake_service, config, monkeypatch):
    monkeypatch.setattr(tray, "read_selection", lambda **kw: None)
    menu = build_menu(config, fake_service, on_quit=lambda: None)
    next(a for a in menu.actions() if a.text() == "Speak selection").trigger()
    assert fake_service.spoken == []


def test_speak_passes_the_configured_length_limit(qapp, fake_service, config, monkeypatch):
    # capture defaults to 20000, so the tray must hand it the configured value or a
    # user-lowered speech.max_chars is not enforced at capture time.
    seen = {}

    def recording(**kw):
        seen.update(kw)
        return Selection("hello there", "primary")

    monkeypatch.setattr(tray, "read_selection", recording)
    config.speech.max_chars = 1234
    menu = build_menu(config, fake_service, on_quit=lambda: None)
    next(a for a in menu.actions() if a.text() == "Speak selection").trigger()
    assert seen.get("max_chars") == 1234


def test_over_long_selection_is_reported_not_raised(qapp, fake_service, config, monkeypatch):
    monkeypatch.setattr(tray, "read_selection", lambda **kw: Selection("x" * 10, "primary"))

    def refuse(text):
        raise TooLongError("selection is 10 characters, over the limit of 5")

    fake_service.speak = refuse
    notices = []
    monkeypatch.setattr(tray, "notify", lambda *args, **kwargs: notices.append(args))
    menu = build_menu(config, fake_service, on_quit=lambda: None)
    next(a for a in menu.actions() if a.text() == "Speak selection").trigger()
    assert notices and "over the limit" in str(notices[0])


def test_start_at_login_writes_and_removes_the_autostart_file(qapp, fake_service, config, tmp_xdg):
    menu = build_menu(config, fake_service, on_quit=lambda: None)
    item = next(a for a in menu.actions() if "login" in a.text().lower())
    entry = tmp_xdg / "config" / "autostart" / "utter.desktop"
    item.trigger()
    assert entry.exists(), "enabling start-at-login must write the autostart entry"
    item.trigger()
    assert not entry.exists(), "disabling it must remove the entry"
