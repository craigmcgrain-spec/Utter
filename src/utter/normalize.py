"""Rewrite the input forms espeak-ng demonstrably misreads before synthesis.

Three classes of text are fixed:

* currency symbols/codes (``$5.99`` -> ``five dollars and ninety-nine cents``, and the same
  amount scaled by a spelled-out magnitude: ``$1.5 million`` -> ``one point five million
  dollars``),
* digit-group commas (``1,500,000`` -> ``1500000``),
* leading-dot decimals (``.5`` -> ``0.5``).

Everything else is a pure pass-through: plain decimals, percentages, ordinals,
signed integers, thousands written without separators, abbreviations, slashes,
clock times, the cent sign, Roman numerals and dates are all left byte-identical. So is an
amount immediately followed by a letter, which is not an amount: ``$1e5`` and ``$5k`` keep
their text, because ``$1e5`` used to come out as ``one dollare5``.

Currency is phrased here over :func:`num2words.num2words` cardinals rather than
its ``to="currency"`` mode, which reads a bare integer amount as cents
(``$5`` -> ``zero dollars, five cents``) and has no yen.
"""

import re

from num2words import num2words

# symbol or code -> (major unit, minor unit or None)
CURRENCY = {
    "$": ("dollar", "cent"),
    "US$": ("dollar", "cent"),
    "USD": ("dollar", "cent"),
    "£": ("pound", "penny"),
    "GBP": ("pound", "penny"),
    "€": ("euro", "cent"),
    "EUR": ("euro", "cent"),
    "¥": ("yen", None),
    "JPY": ("yen", None),
}

SYMBOL_PREFIX = r"(?<![\w$])(US\$|USD|GBP|EUR|JPY|\$|£|€|¥)[ \t]*"
AMOUNT = r"(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)"
SCALE = r"(?i:million|billion|trillion|thousand)"

# The trailing lookahead is what keeps a letter glued to an amount from being read as the
# amount: without it ``$1e5`` matched ``$1`` and came out ``one dollare5``. ``\.\d`` is there
# so the amount cannot stop short of a longer number (``$1.5e3`` must not match just ``$1``).
SYMBOL = re.compile(SYMBOL_PREFIX + "(" + AMOUNT + r")(?!\d|[A-Za-z]|\.\d)")

# ``$1.5 million``: an amount scaled by a spelled-out magnitude. Applied before SYMBOL, which
# would otherwise read the amount as dollars-and-cents and leave `` million`` stranded.
SCALED = re.compile(SYMBOL_PREFIX + "(" + AMOUNT + r")[ \t]*(" + SCALE + r")\b")

GROUPED = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
LEADING_DOT = re.compile(r"(?<![\w.])(\.)(\d)")


def _plural(word: str, count: int) -> str:
    if count == 1:
        return word
    if word == "penny":
        return "pence"
    if word == "yen":
        return "yen"
    return word + "s"


def _amount_words(symbol: str, amount: str, scale: str = "") -> str:
    major, minor = CURRENCY[symbol.upper() if symbol.isupper() or "$" in symbol else symbol]
    value = amount.replace(",", "")
    whole, _, fraction = value.partition(".")
    count_major = int(whole or 0)
    if scale:
        # A scale word multiplies the amount, so the fraction is a decimal rather than cents
        # ("$1.5 million" is not one dollar and fifty cents million) and the unit is always
        # plural: the quantity is a multiple of the unit's own name, so "one million dollars",
        # never "one million dollar" -- hence the count of 2 rather than count_major.
        words = num2words(count_major)
        digits = fraction.rstrip("0")
        if digits:
            words += " point " + " ".join(num2words(int(digit)) for digit in digits)
        return "%s %s %s" % (words, scale.lower(), _plural(major, 2))
    words = "%s %s" % (num2words(count_major), _plural(major, count_major))
    if fraction and minor:
        count_minor = int((fraction + "00")[:2])
        if count_minor:
            words += " and %s %s" % (num2words(count_minor), _plural(minor, count_minor))
    return words


def _currency(match: re.Match) -> str:
    """Phrase one currency match, or leave it exactly as it was when that is not possible.

    ``num2words`` gives up on very large cardinals (``OverflowError``) and Python refuses
    ``int()`` on a string past its 4300-digit limit (``ValueError``), so a selection carrying an
    absurd-but-typeable amount -- a pasted id with a currency sign in front of it -- would
    otherwise raise out of ``normalize()``, and therefore out of ``speak()``, on whichever
    thread called it. Leaving the text alone keeps this a pure pass-through for everything the
    three documented classes do not cover.
    """
    try:
        return _amount_words(match.group(1), match.group(2))
    except (ArithmeticError, ValueError):
        return match.group(0)


def _scaled_currency(match: re.Match) -> str:
    """Phrase ``$1.5 million``/``£4 thousand``, on the same terms as :func:`_currency`.

    The failure mode is identical -- ``int()`` on a 4300-digit amount raises -- so text this
    cannot phrase is left exactly as it was rather than raising out of ``normalize()``.
    """
    try:
        return _amount_words(match.group(1), match.group(2), match.group(3))
    except (ArithmeticError, ValueError):
        return match.group(0)


def normalize(text: str) -> str:
    """Return ``text`` with the three misread forms rewritten."""
    text = SCALED.sub(_scaled_currency, text)
    text = SYMBOL.sub(_currency, text)
    text = GROUPED.sub("", text)
    text = LEADING_DOT.sub(r"0\g<0>", text)
    return text
