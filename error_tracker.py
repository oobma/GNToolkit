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

    def __init__(self) -> None:
        self._count: int = 0

    def record(self, msg: str, *, level: str = "ERROR") -> None:
        """Record one error and print it to the console."""
        self._count += 1
        print(f"[{level}] {msg}")

    @property
    def count(self) -> int:
        return self._count

    @property
    def has_errors(self) -> bool:
        return self._count > 0
