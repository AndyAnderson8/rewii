"""The one base class for errors ReWii raises on purpose.

Every error the core defines derives from :class:`ReWiiError`: its message is a
plain sentence about the user's files, drive, console or network - not a bug.
The CLI prints such errors as one ``error:`` line and the app shows them in a
dialog; anything else (apart from ``OSError``) is a programming error and keeps
its traceback.

Each module still defines its own subclass (``WadError``, ``GameError``...).
A few also keep a built-in second base (``ValueError``, ``KeyError``,
``OSError``) so ``except ValueError`` style callers keep working.
:class:`rewii.core.progress.Cancelled` is deliberately *not* an error.
"""
from __future__ import annotations


class ReWiiError(Exception):
    """Base class of every error ReWii reports to the user in plain language."""
