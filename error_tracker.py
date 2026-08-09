# -*- coding: utf-8 -*-
"""
gn_toolkit.error_tracker — Per-operation import error counter.

Replaces the former global ``IMPORT_ERRORS`` mutable int with an
instance that is created once per import operation and passed explicitly
to every function that needs it.
"""

from __future__ import annotations


class ImportErrorTracker:
    """Tracks errors that occur during a single import operation."""

    # DEBUG/DEFAULT_VALUE are informational; only WARN+ count as issues.
    _REPORTED_LEVELS = frozenset({"WARN", "ERROR", "CRITICAL", "CRITICAL ERROR"})

    def __init__(self) -> None:
        self._count: int = 0
        self._issue_count: int = 0

    def record(self, msg: str, *, level: str = "ERROR") -> None:
        """Record one error and print it to the console."""
        self._count += 1
        if level in self._REPORTED_LEVELS:
            self._issue_count += 1
        print(f"[{level}] {msg}")

    @property
    def count(self) -> int:
        return self._count

    @property
    def warn_count(self) -> int:
        return self._issue_count

    @property
    def has_errors(self) -> bool:
        return self._issue_count > 0
