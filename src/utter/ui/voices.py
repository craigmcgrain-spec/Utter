"""Readable names for the pack's voice ids, shared by the overlay and the tray.

The ids the pack ships are opaque: ``af_heart`` is a language, a gender and a speaker
squashed into one string, and a person picking a voice from the tray reads "Heart (female)"
far faster than the id. One helper here rather than one per surface, so the overlay's label
and the tray's menu cannot disagree about what a voice is called. A name is only invented
for an id this code understands -- a shape it does not recognise falls back to the raw id,
because a guessed name is a worse lie than the string the config already holds.
"""

from __future__ import annotations

from collections.abc import Iterable

VOICE_LANGUAGES = {
    "a": "American English",
    "b": "British English",
    "e": "Spanish",
    "f": "French",
    "h": "Hindi",
    "i": "Italian",
    "j": "Japanese",
    "p": "Brazilian Portuguese",
    "z": "Mandarin Chinese",
}
"""The language the leading letter of an id names, in the order the tray groups them."""

VOICE_GENDERS = {"f": "female", "m": "male"}
"""``f`` or ``m``: the second character of an id."""

OTHER_LANGUAGE = "Other"
"""The group an id that this code cannot read is filed under; it is not given a language."""


def _parts(voice_id: str) -> tuple[str, str, str] | None:
    """``(language, gender, name)`` for a well-formed id, ``None`` for one we cannot read."""
    code, separator, name = voice_id.partition("_")
    if len(code) != 2 or not separator or not name.isalnum():
        return None
    language = VOICE_LANGUAGES.get(code[0])
    gender = VOICE_GENDERS.get(code[1])
    if language is None or gender is None:
        return None
    return language, gender, name


def voice_display_name(voice_id: str) -> str:
    """The speaker alone: ``"af_heart"`` -> ``"Heart"``, or the raw id when unreadable."""
    parts = _parts(voice_id)
    return voice_id if parts is None else parts[2].capitalize()


def voice_language(voice_id: str) -> str | None:
    """The language the id's leading letter names, or ``None`` for a shape we cannot read."""
    parts = _parts(voice_id)
    return None if parts is None else parts[0]


def voice_gender(voice_id: str) -> str | None:
    """``"female"`` or ``"male"``, or ``None`` for a shape we cannot read."""
    parts = _parts(voice_id)
    return None if parts is None else parts[1]


def voice_label(voice_id: str) -> str:
    """``"Heart (female)"``: a menu item's text, with the language left to the group."""
    parts = _parts(voice_id)
    return voice_id if parts is None else f"{parts[2].capitalize()} ({parts[1]})"


def voice_description(voice_id: str) -> str:
    """``"Heart — American English, female"``, or the raw id when the shape is unknown."""
    parts = _parts(voice_id)
    if parts is None:
        return voice_id
    language, gender, name = parts
    return f"{name.capitalize()} — {language}, {gender}"


def grouped_voices(voice_ids: Iterable[str]) -> list[tuple[str, list[str]]]:
    """The ids grouped by language, in the pack's own order, each group keeping its input order.

    The tray's voice menu is the only place a voice is chosen and fifty-four flat ids is a
    list nobody reads; the language is what the first character encodes, so it becomes the
    first level. An unreadable id is filed under :data:`OTHER_LANGUAGE` rather than dropped,
    because a voice the pack offers must stay choosable however oddly it is named.
    """
    groups: dict[str, list[str]] = {}
    for voice_id in voice_ids:
        groups.setdefault(voice_language(voice_id) or OTHER_LANGUAGE, []).append(voice_id)
    ordered = [name for name in VOICE_LANGUAGES.values() if name in groups]
    if OTHER_LANGUAGE in groups:
        ordered.append(OTHER_LANGUAGE)
    return [(name, groups[name]) for name in ordered]
