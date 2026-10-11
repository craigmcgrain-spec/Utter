"""File logging setup: rotating log under the state directory, noisy loggers quieted."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import onnxruntime as ort

from . import paths

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
MAX_BYTES = 1024 * 1024
BACKUP_COUNT = 3


def setup_logging(level: str = "info") -> Path:
    """Install the rotating file handler on the 'utter' logger and return the log path."""
    log_path = paths.log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # ``logging.getLevelName`` is not a parser: given an unknown name it *returns* the string
    # "Level verbose", which ``setLevel`` then rejects with a message naming neither the key nor
    # the value it came from. ``General`` already refuses such a name at load time, so this is
    # that same refusal in words that say what is wrong, for any other caller.
    resolved = logging.getLevelNamesMapping().get(str(level).strip().upper())
    if resolved is None:
        raise ValueError(f"unknown log level: {level!r}")

    logger = logging.getLogger("utter")
    logger.setLevel(resolved)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    handler = RotatingFileHandler(
        log_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(handler)

    logging.getLogger("phonemizer").setLevel(logging.ERROR)
    ort.set_default_logger_severity(3)
    return log_path
