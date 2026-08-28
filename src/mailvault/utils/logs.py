"""Reporting a failure to somebody who is not the author of the program."""

from __future__ import annotations

import logging


def log_failure(
    logger: logging.Logger,
    exc: BaseException,
    msg: str,
    *args: object,
) -> None:
    """Report a failure as one line, and leave its traceback to `--verbose`.

    A traceback is written for whoever wrote the program and read by whoever is
    backing up their mail: forty lines of interpreter frames in which the one
    sentence saying what went wrong is the last, and nothing in them names a
    move. It is still the only thing that points at where an unforeseen failure
    happened, so it is not thrown away -- it goes to DEBUG, which is what `-v`
    turns on.

    Whatever the caller writes, the exception ends the line, so a message reads
    `<what was being done> failed: <what happened>`.
    """
    logger.error(f"{msg}: %s", *args, exc, stacklevel=2)
    logger.debug(msg, *args, exc_info=exc, stacklevel=2)
