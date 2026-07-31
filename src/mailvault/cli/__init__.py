from __future__ import annotations

import importlib.metadata
import logging
import pathlib
import sys


def get_version() -> str:
    """Return the installed package version, or 'unknown' when not packaged."""
    try:
        return importlib.metadata.version("mailvault")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def setup_logger(loglevel: int = logging.INFO, logfile: pathlib.Path | None = None):
    logger_format = "%(asctime)s %(levelname)s -- %(message)s"
    if logfile:
        logging.basicConfig(filename=logfile, level=loglevel, format=logger_format)
    else:
        logging.basicConfig(stream=sys.stderr, level=loglevel, format=logger_format)

    # Third-party libraries that are excessively verbose at INFO/DEBUG level.
    # Only suppress when not explicitly asked for verbose output.
    if loglevel > logging.DEBUG:
        for name in ("httpx", "msal", "imapclient"):
            logging.getLogger(name).setLevel(logging.WARNING)
