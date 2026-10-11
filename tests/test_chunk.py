import string

import pytest

from utter.tts.chunk import SplitReason, TooLongError, split_units


def _punct_only(text):
    return all(char in string.punctuation or char.isspace() for char in text)


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


def test_leading_punctuation_piece_is_dropped():
    # The first-unit cut at the leading comma can return "," on its own; the drop
    # gate must run on the pieces actually emitted, not just on the sentence units.
    text = ", this sentence is long enough that the leading comma triggers a cut here"
    units = split_units(text)
    assert units
    assert not any(_punct_only(u.text) for u in units)
    assert " ".join(u.text for u in units).split() == (
        "this sentence is long enough that the leading comma triggers a cut here".split()
    )


def test_one_word_sentence_tail_merges_forward():
    # "mate." is created by the first-unit clause cut and must be folded into the
    # next unit; the unit ending in "leave," keeps its CLAUSE reason.
    text = (
        "Please remember to water all of the plants before you leave, mate. "
        "Second sentence here."
    )
    units = split_units(text)
    assert all(len(u.text.split()) >= 2 for u in units)
    clause_tail = [u for u in units if u.text.endswith("leave,")]
    assert clause_tail and clause_tail[0].reason is SplitReason.CLAUSE


def test_wrapped_tail_does_not_strand_one_word():
    # Wrapping must avoid leaving a single word ("tail.") as its own piece.
    units = split_units("Alpha beta. " + "word " * 16 + "tail.")
    assert all(len(u.text.split()) >= 2 for u in units)
