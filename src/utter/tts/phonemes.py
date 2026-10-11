"""The phonemiser seam: an alternative grapheme-to-phoneme system, without touching the engine.

``Phonemizer`` is the protocol the engine accepts as its optional ``phonemizer`` argument. When
it is ``None`` -- the default, and what :func:`make_phonemizer` returns for the only shipped
config value, ``"espeak"`` -- the engine passes raw text to ``kokoro-onnx`` and the library
phonemises internally. When a ``Phonemizer`` is supplied, the engine calls
``phonemize(unit_text, lang)`` and hands the result back with ``is_phonemes=True`` instead.

Measured on this project's pinned model, supplying those phonemes is **bit-identical** to letting
the library phonemise, so the seam costs nothing on the default path: a deployment that swaps in
another system changes only the phoneme string the engine receives.

The module is deliberately import-light: it must be importable without constructing an espeak
tokenizer and without importing ``kokoro_onnx`` at module level. ``EspeakPhonemizer`` builds the
real tokenizer lazily, on first phonemisation, and tests inject a fake tokenizer so the suite
never loads espeak data.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Phonemizer(Protocol):
    """A grapheme-to-phoneme system the engine can substitute for the library's own.

    The call shape is fixed by the engine: two positional arguments, the text and the language
    code, returning the phoneme string. A structural ``isinstance(obj, Phonemizer)`` check works
    because the protocol is runtime-checkable.
    """

    def phonemize(self, text: str, lang: str) -> str:
        """Return the phoneme string for ``text`` in language ``lang`` (e.g. ``"en-us"``)."""
        ...


class EspeakPhonemizer:
    """The reference implementation of :class:`Phonemizer`, backed by espeak via ``kokoro-onnx``.

    This is the implementation the seam's tests exercise and the one an alternative-phonemizer
    deployment replaces; the default runtime path never constructs it. The tokenizer is built
    lazily on first use so importing this module costs nothing, and the delegation applies the
    same ``preserve_punctuation``/``with_stress`` behaviour the library applies to itself.
    """

    def __init__(self, tokenizer: object | None = None) -> None:
        self._tokenizer = tokenizer

    def phonemize(self, text: str, lang: str) -> str:
        tokenizer = self._tokenizer
        if tokenizer is None:
            from kokoro_onnx.tokenizer import Tokenizer

            tokenizer = self._tokenizer = Tokenizer()
        return tokenizer.phonemize(text, lang)


def make_phonemizer(name: str) -> Phonemizer | None:
    """Resolve the config's ``speech.phonemizer`` value to a phonemiser, or ``None``.

    ``"espeak"`` is the only shipped value and means "the library phonemises internally", which
    the engine selects by receiving ``None``. Any other value is rejected: no other phonemiser
    ships, so a config naming one is an error rather than a silent fallback.
    """
    if name == "espeak":
        return None
    raise ValueError(f"unknown phonemizer: {name!r}")
