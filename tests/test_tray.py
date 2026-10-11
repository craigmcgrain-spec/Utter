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


def test_start_at_login_records_the_key_and_materialises_the_file(
    qapp, fake_service, config, tmp_xdg
):
    # The config key is the one source of truth and the file is its materialisation, so the
    # item must write the key as well as the file -- and a later startup must be able to read
    # the answer back, which is what the round-trip check here pins.
    menu = build_menu(config, fake_service, on_quit=lambda: None, notifier=None)
    item = next(a for a in menu.actions() if "login" in a.text().lower())
    entry = tmp_xdg / "config" / "autostart" / "utter.desktop"
    item.trigger()
    assert config.general.autostart is True, "the key is the intent"
    assert entry.exists(), "enabling start-at-login must write the autostart entry"
    assert Config.load().general.autostart is True, "the setting must survive a restart"

    item.trigger()
    assert config.general.autostart is False
    assert not entry.exists(), "disabling it must remove the entry"
    assert Config.load().general.autostart is False

    # Re-enabling must put the file back, so the item is not a one-way door.
    item.trigger()
    assert entry.exists()


def test_the_autostart_entry_runs_the_installed_launcher_by_absolute_path(tmp_xdg):
    # The installer records the venv binary's absolute path, because `Exec=utter` alone is
    # resolved through the *login session's* PATH, which need not carry the shim the installer
    # puts in ~/.local/bin. An entry the tray creates must not reintroduce that dependency: one
    # that silently fails to start is a login that never happens, with nothing to show for it.
    from utter import paths
    from utter.autostart import autostart_path, reconcile_autostart
    from utter.config import Config, General

    binary = paths.venv_dir() / "bin" / "utter"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")

    assert reconcile_autostart(Config(general=General(autostart=True)), None) is True
    assert f"Exec={binary}\n" in autostart_path().read_text(encoding="utf-8")


def test_the_autostart_entry_falls_back_to_the_path_launcher(tmp_xdg):
    # No installed venv (a checkout run with `uv run`): `utter` through PATH is all there is.
    from utter.autostart import autostart_path, reconcile_autostart
    from utter.config import Config, General

    assert reconcile_autostart(Config(general=General(autostart=True)), None) is True
    assert "Exec=utter\n" in autostart_path().read_text(encoding="utf-8")


def test_a_launcher_path_with_a_space_is_quoted(tmp_xdg, monkeypatch):
    # Exec is parsed as an argv, so an unquoted path with a space makes the session run the
    # program named before the space, or drop the entry altogether. The installer quotes it; the
    # entry the tray writes has to be quoted the same way.
    import utter.autostart as autostart_module
    from utter.autostart import autostart_path, reconcile_autostart
    from utter.config import Config, General

    monkeypatch.setattr(autostart_module, "venv_dir", lambda: tmp_xdg / "venv dir")
    binary = tmp_xdg / "venv dir" / "bin" / "utter"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")

    assert reconcile_autostart(Config(general=General(autostart=True)), None) is True
    assert f'Exec="{binary}"\n' in autostart_path().read_text(encoding="utf-8")


def test_a_percent_in_the_launcher_path_is_doubled(tmp_xdg, monkeypatch):
    # Exec reserves % for field codes, so a literal one has to be doubled: that is the only way
    # the value can name a path that really contains a percent sign.
    import utter.autostart as autostart_module
    from utter.autostart import autostart_path, reconcile_autostart
    from utter.config import Config, General

    monkeypatch.setattr(autostart_module, "venv_dir", lambda: tmp_xdg / "venv%20dir")
    binary = tmp_xdg / "venv%20dir" / "bin" / "utter"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")

    assert reconcile_autostart(Config(general=General(autostart=True)), None) is True
    assert f'Exec="{tmp_xdg}/venv%%20dir/bin/utter"\n' in autostart_path().read_text(
        encoding="utf-8"
    )


def test_the_item_check_follows_the_key_not_the_file(qapp, fake_service, tmp_xdg):
    # A file left over from an earlier setting must not make the menu claim start-at-login is
    # on: the check derives from the key, and reconciliation is what removes the stale file.
    from utter import autostart as autostart_module
    from utter.autostart import autostart_path

    autostart_path().parent.mkdir(parents=True, exist_ok=True)
    autostart_path().write_text("stale\n", encoding="utf-8")
    config = Config.load()  # autostart defaults to False
    menu = build_menu(config, fake_service, on_quit=lambda: None, notifier=None)
    item = next(a for a in menu.actions() if "login" in a.text().lower())
    assert item.isChecked() is False, "the check must read the key, not the file's existence"
    assert autostart_module.reconcile_autostart(config, None) is True
    assert not autostart_path().exists(), "the stale file must be removed to match the key"


def test_reconcile_autostart_reports_an_unwritable_directory(tmp_xdg, monkeypatch):
    # A key that cannot be honoured is reported, not silently ignored: a login that never
    # happens is exactly what a swallowed OSError would hide. A regular file where the
    # autostart directory must go makes the write fail with an OSError the way a read-only
    # config home would, without monkeypatching the filesystem calls themselves.
    import utter.ui.notify as notify_module
    from utter import autostart as autostart_module
    from utter.autostart import autostart_path
    from utter.config import Config, General

    blocked = tmp_xdg / "blocked"
    blocked.write_text("not a directory\n", encoding="utf-8")
    monkeypatch.setattr(autostart_module, "autostart_path",
                        lambda: blocked / "autostart" / "utter.desktop")
    notified = []
    monkeypatch.setattr(notify_module, "notify",
                        lambda app, message, **kw: notified.append((app, message)))

    config = Config(general=General(autostart=True))
    assert autostart_module.reconcile_autostart(config, "tray-icon") is False
    assert notified and "autostart" in notified[0][1].lower()
    assert notified[0][0] == "tray-icon", "the failure must reach the notifier the caller passed"
