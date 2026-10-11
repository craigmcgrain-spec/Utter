import logging
from logging.handlers import RotatingFileHandler

import pytest

from utter import paths
from utter.logging import setup_logging


def test_log_file_is_created_under_the_state_directory(tmp_xdg):
    assert setup_logging("info") == paths.log_path()
    logging.getLogger("utter").info("hello-marker-12345")
    assert paths.log_path().exists()
    assert "hello-marker-12345" in paths.log_path().read_text()


def test_rotating_handler_size_matches_spec(tmp_xdg):
    setup_logging("info")
    handler = logging.getLogger("utter").handlers[0]
    assert isinstance(handler, RotatingFileHandler)
    assert handler.maxBytes == 1024 * 1024 and handler.backupCount == 3


def test_noisy_phonemizer_logger_is_raised_to_error(tmp_xdg):
    setup_logging("info")
    assert logging.getLogger("phonemizer").level == logging.ERROR


def test_an_unknown_level_is_refused_by_name(tmp_xdg):
    # ``getLevelName`` is not a parser: an unknown name comes back as the string "Level verbose",
    # and setLevel then rejects it with a message that names neither the key nor the value.
    with pytest.raises(ValueError, match="verbose"):
        setup_logging("verbose")


def test_the_configured_level_is_the_one_installed(tmp_xdg):
    setup_logging("debug")
    assert logging.getLogger("utter").level == logging.DEBUG
    assert "debug-marker-54321" not in paths.log_path().read_text()
    logging.getLogger("utter").debug("debug-marker-54321")
    assert "debug-marker-54321" in paths.log_path().read_text()
