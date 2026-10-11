import pytest

from utter import paths
from utter.config import Config, Speech


def test_defaults_match_spec(tmp_xdg):
    config = Config.load()
    assert config.speech.voice == "af_heart"
    assert config.speech.speed == 1.0
    assert config.speech.max_chars == 20000
    assert config.speech.pause_sentence == 0.25
    assert config.speech.pause_clause == 0.12
    assert config.hotkey.speak == "LOGO+ALT+s"
    assert config.ui.show_overlay is True


def test_round_trip_preserves_values(tmp_xdg):
    Config(speech=Speech(voice="bf_emma", speed=1.5)).save()
    loaded = Config.load()
    assert (loaded.speech.voice, loaded.speech.speed) == ("bf_emma", 1.5)


def test_env_overrides_file(tmp_xdg, monkeypatch):
    Config(speech=Speech(speed=1.5)).save()
    monkeypatch.setenv("UTTER_SPEECH_SPEED", "2.0")
    assert Config.load().merged_with_env().speech.speed == 2.0


@pytest.mark.parametrize("voice, lang", [
    ("af_heart", "en-us"), ("bm_george", "en-gb"), ("ef_dora", "es"), ("ff_siwis", "fr-fr"),
    ("hf_alpha", "hi"), ("if_sara", "it"), ("jf_alpha", "ja"), ("pf_dora", "pt-br"),
    ("zf_xiaobei", "cmn"), ("xx_unknown", "en-us"),
])
def test_auto_language_follows_the_voice_prefix(voice, lang):
    assert Speech(voice=voice).effective_lang == lang


def test_an_explicit_language_overrides_the_voice():
    assert Speech(voice="ff_siwis", lang="en-gb").effective_lang == "en-gb"


def test_an_env_override_is_not_written_into_the_file(tmp_xdg, monkeypatch):
    # The tray saves the effective config whenever a setting changes; an override must stay
    # per-invocation rather than become the file's value the next time that happens.
    Config(speech=Speech(voice="af_heart", speed=1.5)).save()
    monkeypatch.setenv("UTTER_SPEECH_VOICE", "bf_emma")
    config = Config.load().merged_with_env()
    config.speech.speed = 1.25  # an unrelated change from the tray
    config.save()

    saved = Config.load()
    assert saved.speech.voice == "af_heart"
    assert saved.speech.speed == 1.25


def test_a_setting_changed_over_an_env_override_is_saved(tmp_xdg, monkeypatch):
    Config(speech=Speech(voice="af_heart")).save()
    monkeypatch.setenv("UTTER_SPEECH_VOICE", "bf_emma")
    config = Config.load().merged_with_env()
    config.speech.voice = "am_adam"  # the user picked a voice in the tray
    config.save()

    assert Config.load().speech.voice == "am_adam"


def test_unknown_keys_are_ignored(tmp_xdg):
    paths.config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.config_path().write_text('[speech]\nvoice = "am_michael"\nfuture_key = 1\n')
    assert Config.load().speech.voice == "am_michael"


def test_speed_is_clamped_to_spec_range():
    assert Speech(speed=0.1).speed == 0.75
    assert Speech(speed=3.0).speed == 2.0


def test_unknown_phonemizer_or_backend_is_rejected(tmp_xdg):
    paths.config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.config_path().write_text('[speech]\nphonemizer = "bogus"\n')
    with pytest.raises(ValueError, match="phonemizer"):
        Config.load()
    paths.config_path().write_text('[hotkey]\nbackend = "bogus"\n')
    with pytest.raises(ValueError, match="backend"):
        Config.load()


def test_an_unknown_log_level_is_rejected_like_the_other_bad_values(tmp_xdg):
    # ``logging`` cannot resolve an unknown name: setLevel raises "Unknown level: 'Level
    # verbose'", which names neither the key nor the value, and it does so from the startup path
    # where nothing catches it. The config layer refuses it instead, the way it already refuses
    # an unknown phonemizer or hotkey backend.
    paths.config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.config_path().write_text('[general]\nlog_level = "verbose"\n')
    with pytest.raises(ValueError, match="log level"):
        Config.load()


def test_an_overlay_position_of_the_wrong_length_is_rejected(tmp_xdg):
    # overlay_pos is unpacked as a pair where the overlay is placed (X11); a wrong-length value
    # used to reach the widget as "too many values to unpack", naming nothing the user could fix.
    paths.config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.config_path().write_text("[ui]\noverlay_pos = [10, 20, 30]\n")
    with pytest.raises(ValueError, match="overlay_pos"):
        Config.load()
    paths.config_path().write_text("[ui]\noverlay_pos = [10, 20]\n")
    assert Config.load().ui.overlay_pos == (10, 20)


def test_a_section_that_is_not_a_table_is_refused(tmp_xdg):
    # `speech = 5` used to reach the field loop as a bare TypeError ("argument of type 'int' is
    # not iterable"), which neither `cli.main`'s boundary nor the doctor's own fallback catches,
    # so the report that must work when the config is broken died loading it.
    paths.config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.config_path().write_text("speech = 5\n")
    with pytest.raises(ValueError, match=r"\[speech\]"):
        Config.load()


def test_a_field_of_the_wrong_shape_is_refused_by_name(tmp_xdg):
    # A list where a float belongs used to leave a TypeError from inside float(); a config that
    # cannot be read has to be the ValueError every caller reports, and the message has to name
    # the key, because the file is the only place the user can fix it.
    paths.config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.config_path().write_text("[speech]\nspeed = [1]\n")
    with pytest.raises(ValueError, match="speed"):
        Config.load()


def test_env_tuple_override_is_parsed(monkeypatch):
    monkeypatch.setenv("UTTER_UI_OVERLAY_POS", "12,34")
    assert Config().merged_with_env().ui.overlay_pos == (12, 34)


def test_env_tuple_override_with_wrong_arity_is_rejected(monkeypatch):
    monkeypatch.setenv("UTTER_UI_OVERLAY_POS", "0")
    with pytest.raises(ValueError, match="UTTER_UI_OVERLAY_POS"):
        Config().merged_with_env()
