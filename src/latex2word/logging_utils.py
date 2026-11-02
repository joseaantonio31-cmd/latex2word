"""Minimal structured logging: ``level: message key=value ...`` on stderr.

Nothing is written to files; stdout is never used so that machine-readable
command output stays clean.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, TextIO

LOGGER_NAME = "latex2word"
_HANDLER_ATTR = "_latex2word_handler"


class StructuredFormatter(logging.Formatter):
    """Format records as ``level: message key=value ...``."""

    def format(self, record: logging.LogRecord) -> str:
        line = f"{record.levelname.lower()}: {record.getMessage()}"
        fields: dict[str, Any] | None = getattr(record, "fields", None)
        if fields:
            line += " " + " ".join(f"{key}={_format_value(value)}" for key, value in fields.items())
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def _format_value(value: object) -> str:
    text = str(value)
    if text == "" or any(ch.isspace() or ch in "\"=" for ch in text):
        return json.dumps(text)
    return text


def log_event(logger: logging.Logger, level: int, message: str, **fields: object) -> None:
    """Emit ``message`` with structured ``fields`` appended as ``key=value``."""
    if logger.isEnabledFor(level):
        logger.log(level, message, extra={"fields": fields})


def level_for(verbosity: int, quiet: bool) -> int:
    """Map CLI flags to a logging level (default WARNING)."""
    if quiet:
        return logging.ERROR
    if verbosity <= 0:
        return logging.WARNING
    if verbosity == 1:
        return logging.INFO
    return logging.DEBUG


def configure_logging(*, verbosity: int = 0, quiet: bool = False, stream: TextIO | None = None) -> None:
    """(Re)configure the ``latex2word`` logger; safe to call repeatedly."""
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_ATTR, False):
            logger.removeHandler(handler)
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(StructuredFormatter())
    setattr(handler, _HANDLER_ATTR, True)
    logger.addHandler(handler)
    logger.setLevel(level_for(verbosity, quiet))
    logger.propagate = False
