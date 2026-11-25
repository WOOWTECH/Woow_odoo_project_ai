# -*- coding: utf-8 -*-
"""How the module logs an unexpected failure (contract v1.2 3.9: every handler
that logs an exception): the exception's type name and the code frames, never
its text, which may carry a key, a header, a URL, an upstream answer, a record's
name or a customer's words. No logger call of the module passes ``exc_info``
or calls ``.exception`` (tests/test_r7_review.py scans for them). No Odoo
import: every module of the addon can use it."""

import logging
import traceback


def log_unexpected(logger, message, args, error, level=logging.ERROR):
    """Log ``message % args`` for the unexpected failure `error` with its type
    name and where it happened (the code lines of its frames, not their
    values), never its text."""
    frames = ''.join(traceback.format_tb(error.__traceback__)).rstrip()
    logger.log(level, message + ' (%s)\n%s', *args, type(error).__name__, frames)
