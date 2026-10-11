import pytest

from utter.tts.chunk import SplitReason, TooLongError, split_units


def test_first_unit_is_short_and_clause_bounded():
    units = split_units("In short, this sentence is long enough to need splitting later.")
    assert units[0].text == "In short,"
    assert units[0].reason is SplitReason.CLAUSE


def test_sentences_become_units():
    units = split_units("One thing. Two things. Three things.")
    assert [u.text for u in units] == ["One thing.", "Two things.", "Three things."]
    assert all(u.reason is SplitReason.SENTENCE for u in units)


def test_long_sentence_wraps_at_eighty_characters():
    text = "word " * 60
    units = split_units(text)
    assert all(len(u.text) <= 80 for u in units)
    assert any(u.reason is SplitReason.WRAP for u in units)


def test_single_word_fragments_merge_into_neighbour():
    # "Yes." is one word and would be spoken as a choppy unit on its own; two-word
    # sentences must still stand alone, which test_sentences_become_units pins.
    units = split_units("Yes. The following sentence is comfortably long enough to stand alone.")
    assert not any(u.text.strip() == "Yes." for u in units)
    assert units[0].text.startswith("Yes.")
    assert len(units[0].text) <= 60


def test_punctuation_only_text_produces_no_units():
    assert split_units("...") == []
    assert split_units("   ") == []


def test_emoji_only_text_is_still_spoken():
    # espeak-ng reads emoji as their names, so these are real units, not noise (spec §2.3).
    assert [u.text for u in split_units("🎉🎉")] == ["🎉🎉"]


def test_unbroken_token_still_wraps():
    units = split_units("a" * 300)
    assert "".join(u.text for u in units) == "a" * 300
    assert all(len(u.text) <= 80 for u in units)


def test_too_long_input_is_refused_before_synthesis():
    with pytest.raises(TooLongError):
        split_units("word " * 5000, max_chars=1000)
