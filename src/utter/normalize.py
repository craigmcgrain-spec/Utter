"""Rewrite the input forms espeak-ng demonstrably misreads before synthesis.

Three classes of text are fixed:

* currency symbols/codes (``$5.99`` -> ``five dollars and ninety-nine cents``),
* digit-group commas (``1,500,000`` -> ``1500000``),
* leading-dot decimals (``.5`` -> ``0.5``).

Everything else is a pure pass-through: plain decimals, percentages, ordinals,
signed integers, thousands written without separators, abbreviations, slashes,
clock times, the cent sign, Roman numerals and dates are all left byte-identical.

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

SYMBOL = r"(?<![\w$])(US\$|USD|GBP|EUR|JPY|\$|£|€|¥)\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)(?!\d)"
GROUPED = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
LEADING_DOT = re.compile(r"(?<![\w.])(\.)(\d)")


def _plural(word: str, count: int) -> str:
    if count == 1:
        return word
    if word == "penny":
        return "pence"
    return word + "s"


def _amount_words(symbol: str, amount: str) -> str:
    major, minor = CURRENCY[symbol.upper() if symbol.isupper() or "$" in symbol else symbol]
    value = amount.replace(",", "")
    whole, _, fraction = value.partition(".")
    count_major = int(whole or 0)
    words = "%s %s" % (num2words(count_major), _plural(major, count_major))
    if fraction and minor:
        count_minor = int((fraction + "00")[:2])
        if count_minor:
            words += " and %s %s" % (num2words(count_minor), _plural(minor, count_minor))
    return words


def normalize(text: str) -> str:
    """Return ``text`` with the three misread forms rewritten."""
    text = re.sub(SYMBOL, lambda match: _amount_words(match.group(1), match.group(2)), text)
    text = GROUPED.sub("", text)
    text = LEADING_DOT.sub(r"0\g<0>", text)
    return text
