import pytest

from utter.normalize import normalize

FIXED = [
    ("$0.00", "zero dollars"),
    ("$5", "five dollars"),
    ("$5.99", "five dollars and ninety-nine cents"),
    ("$0", "zero dollars"),
    ("$1.50", "one dollar and fifty cents"),
    ("US$5", "five dollars"),
    ("£5", "five pounds"),
    ("€5", "five euros"),
    ("3,000", "3000"),
    ("1,500,000", "1500000"),
    ("1,000", "1000"),
    (".5", "0.5"),
    ("$1000", "one thousand dollars"),
    ("$1234", "one thousand, two hundred and thirty-four dollars"),
    ("$.5", "zero dollars and fifty cents"),
    ("$  5", "five dollars"),
    ("¥5", "five yen"),
    ("¥1", "one yen"),
    ("£5.99", "five pounds and ninety-nine pence"),
    ("GBP5", "five pounds"),
    ("EUR5", "five euros"),
]

UNTOUCHED = [
    "3000", "0.5", "1.5", "-3", "+5", "5%", "0.5%", "1st", "21st", "1999", "2024",
    "1.5 million", "99¢", "12:30", "Dr. Smith", "Etc.", "A/B", "5 dollars",
    "1999 was a year", "The value is 42.", "No. 5", "e.g. this",
    "$\n5", "$", "cost $",
]


@pytest.mark.parametrize("text,expected", FIXED)
def test_replaces_forms_espeak_misreads(text, expected):
    assert normalize(text) == expected


@pytest.mark.parametrize("text", UNTOUCHED)
def test_leaves_correct_text_byte_identical(text):
    assert normalize(text) == text


def test_currency_keeps_cents_only_when_written():
    assert normalize("$5.00") == "five dollars"
    assert normalize("$5.05") == "five dollars and five cents"


def test_amount_is_ready_before_chunking():
    assert normalize("It costs $1,234.56 today") == "It costs one thousand, two hundred and thirty-four dollars and fifty-six cents today"
