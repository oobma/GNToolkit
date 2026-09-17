# -*- coding: utf-8 -*-
"""
gn_toolkit.file_utils — shared file-system helpers.

``write_json_file`` performs an atomic JSON write: the data goes to a
temporary file in the destination directory and is swapped into place
with ``os.replace``, so an interrupted write (crash, full disk, killed
process) can never truncate the previous good copy — the JSON files are
the source of truth, and a plain ``open(path, 'w')`` destroys them
before the new content is complete.
"""

from __future__ import annotations

import json
import os
import tempfile


def _remove_quietly(path):
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


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
