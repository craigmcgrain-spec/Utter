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
