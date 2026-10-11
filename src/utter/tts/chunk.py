"""Turn one normalised selection into the synthesis units fed to the engine in turn.

Unit size bounds the two things synthesis cannot interrupt: the time to first
sound (so the first unit is deliberately short) and the worst-case latency when
the user presses the hotkey again mid-utterance. This is pure text geometry --
no configuration, no filesystem, no logging, no engine.

The pipeline, with both gates applied to the pieces that actually get emitted:

1. refuse input longer than ``max_chars`` before doing any splitting work;
2. cut at sentence terminators ``[.!?…]`` and at newlines;
3. drop pieces that are nothing but punctuation or whitespace -- a
   punctuation-only run synthesises as near-silence, while emoji are kept
   because espeak-ng speaks their names (never gate on ``str.isalnum()``:
   ``"🎉".isalnum()`` is ``False`` in Python 3.12);
4. merge pieces of fewer than two words into a neighbour, so the first unit
   measured next is the whole first utterance rather than a stray fragment;
5. bound the first piece for time-to-first-sound: the earliest clause (then
   word) boundary at or before ``first_chars`` that leaves at least two words in
   the head. The two-word minimum wins over the character bound, so a boundary
   that reaches two words past ``first_chars`` is used when nothing fits, and a
   first piece of fewer than two words is left whole for the merge;
6. wrap anything still longer than ``unit_chars`` at clause marks, then word
   boundaries, then by hard slice, avoiding a cut that strands a one-word tail
   when an earlier word boundary avoids it;
7. re-apply the punctuation-only drop, then merge fragments again.

Steps 3 and 7 both exist on purpose: step 3 keeps the common path cheap, while
step 7 is the gate that actually holds, because steps 5 and 6 can manufacture a
new punctuation-only or one-word piece. Steps 4 and 7 are the same merge: step 4
runs before the first unit is measured, step 7 catches fragments that bounding
and wrapping created afterwards.

A unit's ``reason`` comes from its own last non-space character: ``.!?…`` is
``SENTENCE``, ``,;:`` is ``CLAUSE``, anything else is ``WRAP``.
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


@dataclass(frozen=True)
class _Piece:
    """A working piece, before the final gate.

    ``hard`` marks a fragment sliced out of an unbroken token. Such a fragment
    is never merged: joining it to a neighbour would insert a space that was
    never in the source and break the invariant that the units rejoin to the
    original text.
    """

    text: str
    hard: bool = False


def _reason(text: str) -> SplitReason:
    """Classify a unit by the last non-space character it contains."""
    last = text.rstrip()[-1]
    if last in SENTENCE_MARKS:
        return SplitReason.SENTENCE
    if last in CLAUSE_MARKS:
        return SplitReason.CLAUSE
    return SplitReason.WRAP


def _speakable(text: str) -> bool:
    """True unless the piece is nothing but punctuation and whitespace.

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


def _word_count(text: str) -> int:
    return len(text.split())


def _no_strand_cut(text: str, cut: int) -> int:
    """Back a word-boundary cut off so the remainder is never a single word.

    ``cut`` is a space index: the head is ``text[:cut]`` and the remainder starts
    at that space. If the remainder would be one word and an earlier word
    boundary exists, cut there instead -- a stranded word is spoken as an
    isolated, choppy unit, while a longer remainder is not.
    """
    if _word_count(text[cut:]) != 1:
        return cut
    earlier = text.rfind(" ", 0, cut)
    return earlier if earlier > 0 else cut


def _clause_cut(text: str, index: int) -> tuple[str, str]:
    """Split after the clause mark at ``index``."""
    return text[: index + 1], text[index + 1 :].lstrip()


def _word_cut(text: str, cut: int) -> tuple[str, str]:
    """Split at the space at ``cut``, backing off to avoid a one-word tail."""
    cut = _no_strand_cut(text, cut)
    return text[:cut].rstrip(), text[cut:].lstrip()


def _bound_cut(text: str, first_chars: int) -> int | None:
    """Head length at which to cut the first unit, or None to leave it whole.

    Clause boundaries are preferred over word boundaries, as before. The cut
    must leave at least two words in the head: a one-word head would be merged
    into its neighbour by the final merge, undoing the bound and pushing the
    first unit past both ``first_chars`` and ``unit_chars``. So when no boundary
    at or before ``first_chars`` reaches two words, the earliest boundary that
    does is used even though it exceeds ``first_chars`` -- the two-word minimum
    wins over the character bound.
    """
    clauses = [index + 1 for index, char in enumerate(text) if char in CLAUSE_MARKS]
    words = [index for index, char in enumerate(text) if char.isspace()]
    for group in (clauses, words):
        for cut in group:
            if cut <= first_chars and _word_count(text[:cut]) >= 2:
                return cut
    for cut in sorted(clauses + words):
        if _word_count(text[:cut]) >= 2:
            return cut
    return None


def _last_clause(text: str, limit: int) -> int:
    """Index of the last clause mark before ``limit``, or -1."""
    return max(text.rfind(mark, 0, limit) for mark in CLAUSE_MARKS)


def _bound_first(text: str, first_chars: int) -> list[_Piece]:
    """Cut the first unit short for time-to-first-sound.

    The cut is the earliest clause boundary at or before ``first_chars`` that
    leaves at least two words in the head, then the earliest such word boundary,
    then the earliest boundary of either kind that reaches two words even though
    it exceeds ``first_chars``. A first unit of fewer than two words is left
    whole for the merge. The two-word minimum deliberately wins over the
    character bound: do not "fix" this back into a hard character limit.
    """
    cut = _bound_cut(text, first_chars)
    if cut is None:
        return [_Piece(text)]
    return [_Piece(text[:cut].rstrip()), _Piece(text[cut:].lstrip())]


def _wrap(text: str, unit_chars: int) -> list[_Piece]:
    """Break an over-long piece at clause marks, then word boundaries, then slices.

    A piece sliced out of an unbroken run, and the remainder that follows it,
    are marked hard so the merge never joins them with an inserted space; only
    a cut at a real space or clause mark resets that, because such a cut is a
    boundary the source actually contains.
    """
    pieces: list[_Piece] = []
    hard = False
    while len(text) > unit_chars:
        index = _last_clause(text, unit_chars)
        if index >= 0:
            head, tail = _clause_cut(text, index)
            pieces.append(_Piece(head))
            text, hard = tail, False
            continue
        cut = text.rfind(" ", 0, unit_chars + 1)
        if cut <= 0:
            pieces.append(_Piece(text[:unit_chars], hard=True))
            text, hard = text[unit_chars:], True
            continue
        head, tail = _word_cut(text, cut)
        pieces.append(_Piece(head))
        text, hard = tail, False
    if text:
        pieces.append(_Piece(text, hard=hard))
    return pieces


def _merge_short(pieces: list[_Piece]) -> list[_Piece]:
    """Fold every piece of fewer than two words into a neighbour.

    A fragment merges forward into the following piece, or backward into the
    previous one when it is last. A merge may push a unit slightly past
    ``unit_chars`` on purpose: the cap governs splitting, not merging, and the
    alternative is an isolated one-word utterance. Fragments hard-sliced out of
    an unbroken token are exempt (see :class:`_Piece`).
    """
    merged: list[_Piece] = []
    pending: _Piece | None = None
    for piece in pieces:
        if pending is not None:
            piece = _Piece(pending.text + " " + piece.text)
            pending = None
        if _word_count(piece.text) < 2 and not piece.hard:
            pending = piece
            continue
        merged.append(piece)
    if pending is not None:
        if merged:
            merged[-1] = _Piece(merged[-1].text + " " + pending.text)
        else:
            merged.append(pending)
    return merged


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
    pieces = [_Piece(part) for part in _sentences(text) if _speakable(part)]
    if not pieces:
        return []
    pieces = _merge_short(pieces)
    pieces = _bound_first(pieces[0].text, first_chars) + pieces[1:]
    pieces = [
        wrapped for piece in pieces for wrapped in _wrap(piece.text, unit_chars)
    ]
    pieces = _merge_short([piece for piece in pieces if _speakable(piece.text)])
    return [Unit(text=piece.text, reason=_reason(piece.text)) for piece in pieces]
