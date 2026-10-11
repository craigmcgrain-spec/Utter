"""Turn one normalised selection into the synthesis units fed to the engine in turn.

Unit size bounds the two things synthesis cannot interrupt: the time to first
sound (so the first unit is deliberately short) and the worst-case latency when
the user presses the hotkey again mid-utterance. This is pure text geometry --
no configuration, no filesystem, no logging, no engine.

The pipeline runs in this order:

1. refuse input longer than ``max_chars`` before doing any splitting work,
2. cut at sentence terminators ``[.!?…]`` and at newlines,
3. drop units that carry no speakable content (punctuation or whitespace only),
4. merge units of fewer than two words into a neighbouring unit,
5. bound the first unit to ``first_chars`` for time-to-first-sound,
6. wrap anything still longer than ``unit_chars``.

A unit is tagged by the boundary it ends at, so a unit cut short at a clause
mark is ``CLAUSE``, one that ends at a sentence mark is ``SENTENCE`` and
anything else (a wrapped word boundary or a hard slice) is ``WRAP``.
"""

import enum
import re
import unicodedata
from dataclasses import dataclass

SENTENCE_MARKS = ".!?…"
CLAUSE_MARKS = ",;:"

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+|\n+")


class SplitReason(enum.Enum):
    """Why a unit ends where it does."""

    SENTENCE = "sentence"
    CLAUSE = "clause"
    WRAP = "wrap"


class TooLongError(ValueError):
    """The selection is longer than ``max_chars`` and was never split."""


@dataclass(frozen=True)
class Unit:
    """One synthesis unit: its text and the boundary that ends it."""

    text: str
    reason: SplitReason


def _reason(text: str) -> SplitReason:
    """Classify a unit by the mark it ends at."""
    last = text[-1]
    if last in SENTENCE_MARKS:
        return SplitReason.SENTENCE
    if last in CLAUSE_MARKS:
        return SplitReason.CLAUSE
    return SplitReason.WRAP


def _speakable(text: str) -> bool:
    """True unless the unit is nothing but punctuation and whitespace.

    espeak-ng reads symbols such as emoji as their names, so emoji are real
    units rather than noise; only pure punctuation or whitespace is silent.
    """
    return any(
        not char.isspace() and not unicodedata.category(char).startswith("P")
        for char in text
    )


def _sentences(text: str) -> list[str]:
    """Cut at sentence terminators and newlines, dropping blank pieces."""
    return [part.strip() for part in _SENTENCE_SPLIT.split(text) if part.strip()]


def _merge_short(units: list[str]) -> list[str]:
    """Fold every unit of fewer than two words into the following unit.

    A one-word fragment interrupts prosody; a two-word unit does not. A trailing
    fragment with no following unit is appended to the previous one instead.
    """
    merged: list[str] = []
    pending: str | None = None
    for text in units:
        if pending is not None:
            text = pending + " " + text
            pending = None
        if len(text.split()) < 2:
            pending = text
            continue
        merged.append(text)
    if pending is not None:
        if merged:
            merged[-1] = merged[-1] + " " + pending
        else:
            merged.append(pending)
    return merged


def _bound_first(text: str, first_chars: int) -> list[str]:
    """Cut the first unit short for time-to-first-sound.

    A first sentence already shorter than ``first_chars`` is left whole.
    Otherwise cut at the first clause mark at or before ``first_chars``, falling
    back to the last word boundary at or before it.
    """
    if len(text) <= first_chars:
        return [text]
    for index, char in enumerate(text[:first_chars]):
        if char in CLAUSE_MARKS:
            return [text[: index + 1], text[index + 1 :].strip()]
    cut = text.rfind(" ", 0, first_chars + 1)
    if cut <= 0:
        cut = first_chars
    return [text[:cut].rstrip(), text[cut:].strip()]


def _wrap(text: str, unit_chars: int) -> list[str]:
    """Break an over-long unit at word boundaries, hard-slicing an unbroken token."""
    pieces: list[str] = []
    while len(text) > unit_chars:
        cut = text.rfind(" ", 0, unit_chars + 1)
        if cut <= 0:
            pieces.append(text[:unit_chars])
            text = text[unit_chars:]
            continue
        pieces.append(text[:cut].rstrip())
        text = text[cut + 1 :].lstrip()
    if text:
        pieces.append(text)
    return pieces


def split_units(
    text: str,
    *,
    first_chars: int = 60,
    unit_chars: int = 80,
    max_chars: int = 20000,
) -> list[Unit]:
    """Return the synthesis units for one normalised selection.

    Raises :class:`TooLongError` when ``text`` exceeds ``max_chars``.
    """
    if len(text) > max_chars:
        raise TooLongError(
            "selection is %d characters, over the %d limit" % (len(text), max_chars)
        )
    units = [part for part in _sentences(text) if _speakable(part)]
    units = _merge_short(units)
    if not units:
        return []
    pieces = _bound_first(units[0], first_chars) + units[1:]
    wrapped = [piece for unit in pieces for piece in _wrap(unit, unit_chars) if piece]
    return [Unit(text=piece, reason=_reason(piece)) for piece in wrapped]
