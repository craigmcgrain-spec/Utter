"""The tray menu drives the engine, and its invisible contracts hold.

A reader who only checks the menu labels would miss several: the Speak action must hand
capture the *configured* ``max_chars`` (capture defaults to the literal 20000) and must
turn a refusal into a notification because an exception escaping a Qt slot is a crash,
not an error path; the voice and speed items must persist their choice; and a config file
that cannot be written must be reported, not raised.
"""

import utter.ui.tray as tray
from utter.capture import Selection
from utter.config import Config
from utter.tts.chunk import TooLongError
from utter.ui.tray import build_menu


def test_menu_contains_every_control(qapp, fake_service, config):
    menu = build_menu(config, fake_service, on_quit=lambda: None, notifier=None)
    labels = [action.text() for action in menu.actions() if action.text()]
    assert "Speak selection" in labels
    assert "Stop" in labels
    assert any("Voice" in label for label in labels)
    assert any("Speed" in label for label in labels)


def test_speak_action_asks_the_service_to_speak_the_selection(qapp, fake_service, config, monkeypatch):
    monkeypatch.setattr(tray, "read_selection", lambda **kw: Selection("hello there", "primary"))
    menu = build_menu(config, fake_service, on_quit=lambda: None, notifier=None)
    next(a for a in menu.actions() if a.text() == "Speak selection").trigger()
    assert fake_service.spoken == ["hello there"]


def test_speak_with_no_selection_notifies_once(qapp, fake_service, config, monkeypatch):
    monkeypatch.setattr(tray, "read_selection", lambda **kw: None)
    notices = []
    monkeypatch.setattr(tray, "notify", lambda *args, **kwargs: notices.append(args))
    menu = build_menu(config, fake_service, on_quit=lambda: None, notifier=None)
    next(a for a in menu.actions() if a.text() == "Speak selection").trigger()
    assert fake_service.spoken == []
    assert len(notices) == 1, "the user must be told there was nothing to speak"


def test_choosing_a_voice_persists_it(qapp, fake_service, config, tmp_xdg):
    menu = build_menu(config, fake_service, on_quit=lambda: None, notifier=None)
    voice_menu = next(a.menu() for a in menu.actions() if a.menu() and "Voice" in a.text())
    voice_menu.actions()[1].trigger()
    saved = Config.load()
    assert saved.speech.voice == config.speech.voice != "af_heart"


def test_a_failed_save_notifies_instead_of_raising(qapp, fake_service, config, monkeypatch):
    notices = []
    monkeypatch.setattr(tray, "notify", lambda *args, **kwargs: notices.append(args))
    monkeypatch.setattr(Config, "save", lambda self, path=None: (_ for _ in ()).throw(OSError("read-only")))
    menu = build_menu(config, fake_service, on_quit=lambda: None, notifier=None)
    next(a for a in menu.actions() if "Speed" in a.text()).menu().actions()[2].trigger()
    assert notices, "a failed save must be reported, not raised out of a Qt slot"


def test_speak_passes_the_configured_length_limit(qapp, fake_service, config, monkeypatch):
    # capture defaults to 20000, so the tray must hand it the configured value or a
    # user-lowered speech.max_chars is not enforced at capture time.
    seen = {}

    def recording(**kw):
        seen.update(kw)
        return Selection("hello there", "primary")

    monkeypatch.setattr(tray, "read_selection", recording)
    config.speech.max_chars = 1234
    menu = build_menu(config, fake_service, on_quit=lambda: None, notifier=None)
    next(a for a in menu.actions() if a.text() == "Speak selection").trigger()
    assert seen.get("max_chars") == 1234


def test_over_long_selection_is_reported_not_raised(qapp, fake_service, config, monkeypatch):
    monkeypatch.setattr(tray, "read_selection", lambda **kw: Selection("x" * 10, "primary"))

    def refuse(text):
        raise TooLongError("selection is 10 characters, over the limit of 5")

    fake_service.speak = refuse
    notices = []
    monkeypatch.setattr(tray, "notify", lambda *args, **kwargs: notices.append(args))
    menu = build_menu(config, fake_service, on_quit=lambda: None, notifier=None)
    next(a for a in menu.actions() if a.text() == "Speak selection").trigger()
    assert notices and "over the limit" in str(notices[0])


def test_start_at_login_writes_and_removes_the_autostart_file(qapp, fake_service, config, tmp_xdg):
    menu = build_menu(config, fake_service, on_quit=lambda: None, notifier=None)
    item = next(a for a in menu.actions() if "login" in a.text().lower())
    entry = tmp_xdg / "config" / "autostart" / "utter.desktop"
    item.trigger()
    assert entry.exists(), "enabling start-at-login must write the autostart entry"
    item.trigger()
    assert not entry.exists(), "disabling it must remove the entry"
