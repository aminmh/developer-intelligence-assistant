"""Centralised exception logging for the Python service.

Every uncaught exception — in the main thread, in worker threads, or raised
inside a FastAPI request handler — is written to a dedicated rotating log file
(``logs/exceptions.log``) instead of dying silently.
"""

from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from infra.settings import settings

EXCEPTION_LOGGER_NAME = "exceptions"


def get_exception_logger() -> logging.Logger:
    """Return the shared exception logger, configuring it on first use."""
    logger = logging.getLogger(EXCEPTION_LOGGER_NAME)
    if not logger.handlers:
        logger.setLevel(logging.ERROR)
        logger.propagate = False

        log_dir = Path(settings.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)

        handler = RotatingFileHandler(
            log_dir / "exceptions.log",
            maxBytes=10_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                "[%(asctime)s] %(levelname)s %(threadName)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        logger.addHandler(handler)
    return logger


def install_exception_hooks() -> None:
    """Route uncaught main-thread and worker-thread exceptions to the log."""
    logger = get_exception_logger()

    def _handle_uncaught(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.error(
            "Uncaught exception",
            exc_info=(exc_type, exc_value, exc_tb),
        )

    def _handle_thread_exception(args: threading.ExceptHookArgs) -> None:
        thread_name = args.thread.name if args.thread else "unknown"
        logger.error(
            "Uncaught exception in thread %s",
            thread_name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _handle_uncaught
    threading.excepthook = _handle_thread_exception
