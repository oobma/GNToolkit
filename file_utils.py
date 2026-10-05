# -*- coding: utf-8 -*-
"""
gn_toolkit.file_utils — shared file-system helpers.

``write_json_file`` performs an atomic JSON write: the data goes to a
temporary file in the destination directory and is swapped into place
with ``os.replace``, so an interrupted write (crash, full disk, killed
process) can never truncate the previous good copy — the JSON files are
the source of truth, and a plain ``open(path, 'w')`` destroys them
before the new content is complete.

``sanitize_filename`` / ``FilenameAllocator`` turn datablock names into
safe file stems. The character policy is exactly what the addon has
always written (alphanumerics, spaces and underscores kept, everything
else becomes ``_``), so file paths of existing folder exports do not
change (verified against the 439 group names of the reference project),
plus the missing guards — an empty/all-space name falls back to
``unnamed``, Windows reserved device names (``CON``, ``NUL``,
``COM1``…) are prefixed (``CON.json`` cannot be created), and names that
collide after cleaning (``A/B`` and ``A:B`` both clean to ``A_B``) get a
short hash suffix instead of silently overwriting each other.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time

_BACKUP_KEEP = 5

_RESERVED_STEMS = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _strip_end_punctuation(value):
    """``value.strip(" .")`` without a multi-character ``str.strip`` call.

    Equivalent to stripping ``'.'`` and ``' '`` from both ends of *value*.
    """
    start = 0
    end = len(value)
    while start < end and value[start] in " .":
        start += 1
    while end > start and value[end - 1] in " .":
        end -= 1
    return value[start:end]


def sanitize_filename(name, fallback="unnamed"):
    """Filesystem-safe stem for *name* (extension added by the caller).

    Not injective: ``sanitize_filename("A/B") == sanitize_filename("A:B")``.
    Use :class:`FilenameAllocator` when several files are written at once.
    """
    stem = "".join(c if c.isalnum() or c in (" ", "_") else "_" for c in name)
    if not _strip_end_punctuation(stem):
        stem = fallback
    if _strip_end_punctuation(stem).upper() in _RESERVED_STEMS:
        stem = "_" + stem
    return stem


class FilenameAllocator:
    """Assign unique file stems within one export batch.

    The cleaned name is used as-is while it is unique; a name that
    collides with an earlier one (cleaning is lossy) gets the cleaned
    name plus ``~<hash of the original>``, and every adjusted name is
    recorded in :attr:`adjusted` as ``(original, assigned)`` so the
    operator can tell the user where the file went.
    """

    def __init__(self):
        self._used = set()
        self.adjusted = []

    def allocate(self, name, fallback="unnamed"):
        stem = sanitize_filename(name, fallback)
        if stem in self._used:
            digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
            candidate = f"{stem}~{digest}"
            dedup = 2
            while candidate in self._used:
                candidate = f"{stem}~{digest}-{dedup}"
                dedup += 1
            stem = candidate
            self.adjusted.append((name, stem))
        elif stem != name:
            self.adjusted.append((name, stem))
        self._used.add(stem)
        return stem


def _remove_quietly(path):
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _prune_backups(backups_root, keep):
    try:
        entries = sorted(
            (name for name in os.listdir(backups_root)
             if os.path.isdir(os.path.join(backups_root, name))),
            reverse=True,
        )
    except OSError:
        return
    for name in entries[keep:]:
        shutil.rmtree(os.path.join(backups_root, name), ignore_errors=True)


def snapshot_files(paths, base_dir, keep=_BACKUP_KEEP):
    """Copy every existing file of *paths* into ``<base_dir>/backups/<stamp>/``.

    Relative layout under *base_dir* is preserved.  Keeps the last *keep*
    backup folders.  Returns ``(backup_dir, errors)``; a copy failure is
    reported to the caller instead of aborting the operation (the
    snapshot is a safety net, not a precondition).
    """
    existing = [p for p in paths if p and os.path.isfile(p)]
    if not existing or not base_dir:
        return "", []
    backups_root = os.path.join(base_dir, "backups")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(backups_root, stamp)
    suffix = 2
    while os.path.isdir(backup_dir):
        backup_dir = os.path.join(backups_root, f"{stamp}-{suffix}")
        suffix += 1

    errors = []
    for src in existing:
        try:
            rel = os.path.relpath(os.path.abspath(src), os.path.abspath(base_dir))
        except ValueError:
            rel = os.path.basename(src)
        if rel.startswith(".."):
            rel = os.path.basename(src)
        try:
            dest = os.path.join(backup_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(src, dest)
        except OSError as exc:
            errors.append(f"{src}: {exc}")
    _prune_backups(backups_root, keep)
    return backup_dir, errors


def write_json_file(json_path, data, dump_args=None):
    """Write *data* as JSON to *json_path*, atomically.

    ``dump_args`` are passed to :func:`json.dump` (defaults to indent=4,
    ensure_ascii=False — the package format). Raises ``PermissionError``
    with a user-friendly message when the destination is not writable.
    """
    if dump_args is None:
        dump_args = {"indent": 4, "ensure_ascii": False}
    directory = os.path.dirname(os.path.abspath(json_path))
    tmp_path = None
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=os.path.basename(json_path) + ".", suffix=".tmp",
            dir=directory,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, **dump_args)
        os.replace(tmp_path, json_path)
        tmp_path = None
    except PermissionError:
        _remove_quietly(tmp_path)
        raise PermissionError(
            f"Cannot write to {json_path} — save the .blend file in a "
            "writable location and try again"
        ) from None
    except Exception:
        _remove_quietly(tmp_path)
        raise
