"""Logging to the console and, optionally, a dated file."""

from __future__ import annotations

import logging
import os
from datetime import datetime


def setup_logging(level: str = "INFO", log_dir: str | None = "logs") -> str | None:
    """Configure the ``isc_reporter`` logger. Returns the log file path, if any."""
    logger = logging.getLogger("isc_reporter")
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    if not log_dir:
        return None
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"isc-reporter_{datetime.now():%Y-%m-%d}.log")
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return path
