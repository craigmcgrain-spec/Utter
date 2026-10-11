import logging
from logging.handlers import RotatingFileHandler

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
