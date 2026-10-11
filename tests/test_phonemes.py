import pytest

from utter.tts.phonemes import EspeakPhonemizer, make_phonemizer


def test_espeak_delegates_to_the_library_tokenizer():
    class FakeTokenizer:
        def phonemize(self, text, lang="en-us", norm=True):
            return "dummy"

    assert EspeakPhonemizer(FakeTokenizer()).phonemize("hi", "en-us") == "dummy"


def test_espeak_forwards_language():
    seen = {}

    class FakeTokenizer:
        def phonemize(self, text, lang="en-us", norm=True):
            seen["lang"] = lang
            return "x"

    EspeakPhonemizer(FakeTokenizer()).phonemize("hi", "en-gb")
    assert seen["lang"] == "en-gb"


def test_default_phonemizer_is_the_library_path():
    assert make_phonemizer("espeak") is None


def test_unknown_phonemizer_is_rejected():
    with pytest.raises(ValueError, match="phonemizer"):
        make_phonemizer("festival")
