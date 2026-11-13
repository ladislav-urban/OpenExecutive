"""Capturing logs for a leak test that can't pass on an empty capture.

``caplog`` listens on the root logger. Once any test on the same worker has
built the app, ``api.main``'s logging setup stops the ``openexecutive``
logger propagating to root, so ``caplog.text`` is empty and an assertion that
a secret is *not* in it passes whatever was logged. ``recording`` attaches
caplog's handler to the named loggers themselves for the block; a leak test
then asserts that something it expects was captured before it asserts what
wasn't."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import pytest


@contextmanager
def recording(caplog: pytest.LogCaptureFixture, *names: str) -> Iterator[None]:
    """caplog's handler on each named logger, at DEBUG, for the block."""
    caplog.handler.setLevel(logging.DEBUG)
    loggers = [logging.getLogger(name) for name in names]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.addHandler(caplog.handler)
        logger.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        for logger, level in zip(loggers, levels, strict=True):
            logger.removeHandler(caplog.handler)
            logger.setLevel(level)
