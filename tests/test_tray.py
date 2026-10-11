"""The tray menu drives the engine, and its invisible contracts hold.

A reader who only checks the menu labels would miss several: the Speak action must hand
capture the *configured* ``max_chars`` (capture defaults to the literal 20000) and must
turn a refusal into a notification because an exception escaping a Qt slot is a crash,
not an error path; the voice and speed items must persist their choice; and a config file
that cannot be written must be reported, not raised. The voice submenu is the one place a
voice is chosen, so what it *names* and what it *stores* are two different things and both
matter: the item reads "Heart (female)" and the config still gets ``af_heart``.
"""

import utter.ui.tray as tray
from conftest import FakeService
from utter.capture import Selection
from utter.config import Config
from utter.tts.chunk import TooLongError
from utter.ui.tray import Tray, build_menu
from utter.ui.voices import voice_description, voice_display_name


def _submenu(menu, prefix):
    """The submenu whose title starts with ``prefix`` (the voice title carries the choice)."""
    return next(a.menu() for a in menu.actions() if a.menu() and a.text().startswith(prefix))


def _voice_items(menu):
    """Every item under the Voice submenu, language submenus included."""
    voice_menu = _submenu(menu, "Voice")
    return [item for language in voice_menu.findChildren(type(voice_menu)) for item in language.actions()]


def _speed_items(menu):
    return _submenu(menu, "Speed").actions()


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


def test_a_failed_save_notifies_instead_of_raising(qapp, fake_service, config, monkeypatch):
    notices = []
    monkeypatch.setattr(tray, "notify", lambda *args, **kwargs: notices.append(args))
    monkeypatch.setattr(Config, "save", lambda self, path=None: (_ for _ in ()).throw(OSError("read-only")))
    menu = build_menu(config, fake_service, on_quit=lambda: None, notifier=None)
    next(a for a in _speed_items(menu) if a.text() == "1.25×").trigger()
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


# -- the voice menu --------------------------------------------------------


def _multi_language_service():
    return FakeService(
        voices=("af_heart", "af_bella", "am_adam", "bf_emma", "jf_gongitsune"), state="IDLE"
    )


def test_the_voice_menu_groups_by_language_and_names_the_speaker(qapp, config):
    # Fifty-four flat ids is a wall nobody reads, so the language -- which the id's first
    # letter already encodes -- becomes the first level, and the item names the speaker
    # rather than the id.
    config.speech.voice = "bf_emma"
    menu = build_menu(config, _multi_language_service(), on_quit=lambda: None, notifier=None)
    voice_menu = _submenu(menu, "Voice")
    languages = [language.title() for language in voice_menu.findChildren(type(voice_menu))]

    assert languages == ["American English", "British English", "Japanese"]
    assert {item.text() for item in _voice_items(menu)} == {
        "Heart (female)",
        "Bella (female)",
        "Adam (male)",
        "Emma (female)",
        "Gongitsune (female)",
    }
    assert {item.data() for item in _voice_items(menu)} == {
        "af_heart",
        "af_bella",
        "am_adam",
        "bf_emma",
        "jf_gongitsune",
    }, "the config stores the id, whatever the item is called"


def test_choosing_a_voice_persists_the_raw_id(qapp, config, tmp_xdg):
    # The menu shows "Emma (female)"; the engine and the config take "bf_emma".
    tray_icon = Tray(_multi_language_service(), config, on_quit=lambda: None)
    menu = tray_icon.contextMenu()
    item = next(a for a in _voice_items(menu) if a.text() == "Emma (female)")
    item.trigger()

    assert config.speech.voice == "bf_emma"
    assert Config.load().speech.voice == "bf_emma", "a chosen voice must survive a restart"


def test_the_voice_check_and_title_follow_the_choice(qapp, config):
    tray_icon = Tray(_multi_language_service(), config, on_quit=lambda: None)
    menu = tray_icon.contextMenu()
    assert _submenu(menu, "Voice").title() == "Voice: Heart", "the title states the current voice"
    assert [a.data() for a in _voice_items(menu) if a.isChecked()] == ["af_heart"]

    next(a for a in _voice_items(menu) if a.text() == "Gongitsune (female)").trigger()

    assert [a.data() for a in _voice_items(menu) if a.isChecked()] == ["jf_gongitsune"]
    assert _submenu(menu, "Voice").title() == "Voice: Gongitsune"


def test_the_voice_check_moves_when_another_surface_sets_the_voice(qapp, config):
    # The overlay does not choose voices, but the config can change under the menu; the check
    # is derived rather than remembered, so it cannot go stale.
    tray_icon = Tray(_multi_language_service(), config, on_quit=lambda: None)
    tray_icon.set_voice("am_adam")
    checked = [a for a in _voice_items(tray_icon.contextMenu()) if a.isChecked()]
    assert [a.data() for a in checked] == ["am_adam"]


def test_a_voice_item_carries_its_language_and_gender_on_hover(qapp, config):
    menu = build_menu(config, _multi_language_service(), on_quit=lambda: None, notifier=None)
    item = next(a for a in _voice_items(menu) if a.text() == "Emma (female)")
    assert "British English" in item.toolTip() and "female" in item.toolTip()


# -- the speed menu --------------------------------------------------------


def test_the_speed_menu_marks_the_current_preset_with_the_times_sign(qapp, fake_service, config):
    config.speech.speed = 1.25
    menu = build_menu(config, fake_service, on_quit=lambda: None, notifier=None)

    texts = {a.text() for a in _speed_items(menu)}
    assert {"0.75×", "1×", "1.25×", "1.5×", "2×"} <= texts
    assert [a.text() for a in _speed_items(menu) if a.isChecked()] == ["1.25×"]


def test_the_speed_menu_shows_a_custom_speed_that_no_preset_matches(qapp, config):
    # The slider commits anything in 0.75-2.0. Leaving every preset unchecked would read as
    # "no speed at all", so the value the engine actually received is shown instead.
    config.speech.speed = 1.37
    tray_icon = Tray(config=config, service=FakeService(state="IDLE"), on_quit=lambda: None)
    menu = tray_icon.contextMenu()

    checked = [a for a in _speed_items(menu) if a.isChecked()]
    assert [a.text() for a in checked] == ["Custom (1.37×)"]
    assert checked[0].isEnabled() is False, "a custom value is chosen on the slider, not here"
    assert _submenu(menu, "Speed").title() == "Speed: 1.37×"

    tray_icon.set_speed(1.5)
    assert [a.text() for a in _speed_items(menu) if a.isChecked()] == ["1.5×"]
    assert _submenu(menu, "Speed").title() == "Speed: 1.5×"

    tray_icon.set_speed(1.37)
    assert [a.text() for a in _speed_items(menu) if a.isChecked()] == ["Custom (1.37×)"]


def test_choosing_a_speed_persists_it(qapp, fake_service, config, tmp_xdg):
    menu = build_menu(config, fake_service, on_quit=lambda: None, notifier=None)
    next(a for a in _speed_items(menu) if a.text() == "2×").trigger()
    assert config.speech.speed == 2.0
    assert Config.load().speech.speed == 2.0


# -- playback state --------------------------------------------------------


def _pause_action(menu):
    return next(a for a in menu.actions() if a.text() in ("Pause", "Resume"))


def _stop_action(menu):
    return next(a for a in menu.actions() if a.text() == "Stop")


def test_pause_and_stop_are_disabled_until_there_is_speech_to_act_on(qapp, config):
    # Resuming silence and stopping silence are not actions a user can want, so the two items
    # are offered only while the engine is playing or paused.
    service = FakeService(state="IDLE")
    tray_icon = Tray(service, config, on_quit=lambda: None)
    menu = tray_icon.contextMenu()
    assert not _pause_action(menu).isEnabled()
    assert not _stop_action(menu).isEnabled()

    tray_icon.set_state("PLAYING")
    assert _pause_action(menu).isEnabled() and _pause_action(menu).text() == "Pause"
    assert _stop_action(menu).isEnabled()

    tray_icon.set_state("PAUSED")
    assert _pause_action(menu).isEnabled() and _pause_action(menu).text() == "Resume"

    tray_icon.set_state("IDLE")
    assert not _pause_action(menu).isEnabled()
    assert not _stop_action(menu).isEnabled()


def test_the_pause_item_toggles_whichever_way_the_engine_is(qapp, config):
    # One item, both directions, decided by the engine's own state rather than by a check the
    # overlay and the hotkey would also have to keep in step.
    service = FakeService(state="PLAYING")
    tray_icon = Tray(service, config, on_quit=lambda: None)
    menu = tray_icon.contextMenu()

    _pause_action(menu).trigger()
    assert service.calls[-1] == "pause"

    service._status["paused"] = True
    tray_icon.set_state("PAUSED")
    _pause_action(menu).trigger()
    assert service.calls[-1] == "resume"


def test_the_tooltip_names_the_state_and_the_voice(qapp, config):
    # The tooltip is the only state cue the tray carries, so it has to say which state and --
    # while speaking -- whose voice it is.
    tray_icon = Tray(FakeService(state="IDLE"), config, on_quit=lambda: None)
    assert "Utter" in tray_icon.toolTip() and "Ready" in tray_icon.toolTip()

    tray_icon.set_state("PLAYING")
    assert "Speaking" in tray_icon.toolTip() and "Heart" in tray_icon.toolTip()

    config.speech.voice = "bf_emma"
    tray_icon.set_state("PLAYING")
    assert "Emma" in tray_icon.toolTip(), "the tooltip follows the voice the engine holds"

    tray_icon.set_state("PAUSED")
    assert "Paused" in tray_icon.toolTip()


def test_the_tray_uses_the_application_icon(qapp, fake_service, config):
    # The generic volume pixmap was the placeholder; the app's own icon is what the installer
    # puts in hicolor, and the packaged copy is the fallback when the theme has not got it.
    from PyQt6.QtWidgets import QApplication, QStyle

    tray_icon = Tray(fake_service, config, on_quit=lambda: None)
    generic = QApplication.style().standardIcon(QStyle.StandardPixmap.SP_MediaVolume)
    assert not tray_icon.icon().isNull()
    assert tray_icon.icon().cacheKey() != generic.cacheKey()
    assert not tray_icon.icon().pixmap(16, 16).isNull(), "the icon must render, not just load"


def test_a_menu_choice_is_announced_for_the_other_surface(qapp, config):
    # The overlay shows the voice and owns the slider's value, so a choice made here has to
    # reach it; the signal is that path, and nothing else in the app needs the raw callback.
    tray_icon = Tray(_multi_language_service(), config, on_quit=lambda: None)
    voices, speeds = [], []
    tray_icon.voice_changed.connect(voices.append)
    tray_icon.speed_changed.connect(speeds.append)

    next(a for a in _voice_items(tray_icon.contextMenu()) if a.text() == "Adam (male)").trigger()
    next(a for a in _speed_items(tray_icon.contextMenu()) if a.text() == "1.5×").trigger()

    assert voices == ["am_adam"]
    assert speeds == [1.5]


# -- the voice-name helpers ------------------------------------------------


def test_an_unreadable_voice_id_falls_back_to_itself():
    # A guessed name for a shape this code does not understand would be worse than the id.
    for unknown in ("nonsense", "x_thing", "qf_odd", "af_", "af_heart_extra_?"):
        assert voice_display_name(unknown) == unknown
        assert voice_description(unknown) == unknown


def test_a_voice_id_is_named_for_its_speaker_language_and_gender():
    assert voice_display_name("af_heart") == "Heart"
    description = voice_description("bf_emma")
    assert "Emma" in description and "British English" in description and "female" in description
    assert "Mandarin Chinese" in voice_description("zf_xiaobei")
    assert "male" in voice_description("zm_yunjian")


# -- start at login --------------------------------------------------------


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
