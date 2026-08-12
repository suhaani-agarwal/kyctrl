"""Application-event logging via loguru — distinct from the SQLite audit
log in `audit.py`. This captures things like "webhook received", "agent
started", "config reloaded", "error" — operational noise a maintainer
would tail in a terminal. The audit log captures only agent *decisions*.
"""

from __future__ import annotations

import sys

from loguru import logger


def setup_logging(level: str = "INFO") -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> - <level>{message}</level>",
    )
