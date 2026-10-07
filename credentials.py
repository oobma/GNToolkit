# -*- coding: utf-8 -*-
"""
gn_toolkit.credentials — per-host Git credentials for the embedded engine.

Lookup chain, first hit wins:

  1. session — scripts/tests via ``set_session`` (a "" url key means any
     host; the M3 compatibility shim uses that);
  2. environment — ``GNT_GIT_USERNAME`` / ``GNT_GIT_TOKEN`` (CI, headless);
  3. the OS vault — ``keyring``, service "GNToolkit", account = host;
  4. the add-on preferences — the documented plain-text fallback (a
     single host at a time: ``git_host``/``git_username``/``git_token``);
  5. nothing.

Vault records are JSON strings ``{"username": ..., "token": ...}``; a bare
legacy string is accepted as a token. ``save`` clears the preference
fields when the vault accepted the record, so the secret does not stay in
plain text. Secrets are never logged and never written to project files.

keyring is optional: when it cannot be imported (legacy installs, the
zipimport wheels cannot carry the jaraco namespace packages) or no
backend exists (headless Linux), every operation degrades to the next
level without raising.
"""

from __future__ import annotations

import json
import logging
import os
from urllib.parse import urlsplit

_log = logging.getLogger("GNToolkit.git")

SERVICE = "GNToolkit"

_session = {}
_keyring = None
_keyring_checked = False


def host_for(url):
    """Hostname of *url* ("" for local paths, malformed or missing urls)."""
    if not url:
        return ""
    try:
        host = urlsplit(str(url).strip()).hostname or ""
    except ValueError:
        host = ""
    return host


def _prefs():
    import bpy
    try:
        return bpy.context.preferences.addons[__package__].preferences
    except Exception:
        return None


def _keyring_module():
    global _keyring, _keyring_checked
    if _keyring_checked:
        return _keyring
    _keyring_checked = True
    try:
        import keyring
        _keyring = keyring
    except Exception:
        _keyring = None
    return _keyring


def set_session(url, username, token):
    """Set (or clear, with an empty token) session credentials for *url*."""
    host = host_for(url)
    if token:
        _session[host] = (username or "", token)
    else:
        _session.pop(host, None)


def _from_session(host):
    if host in _session:
        return _session[host] + ("session",)
    if "" in _session:
        return _session[""] + ("session",)
    return None


def _from_env():
    token = os.environ.get("GNT_GIT_TOKEN", "").strip()
    if not token:
        return None
    username = os.environ.get("GNT_GIT_USERNAME", "").strip()
    return (username, token, "environment")


def _from_vault(host):
    keyring = _keyring_module()
    if keyring is None or not host:
        return None
    try:
        raw = keyring.get_password(SERVICE, host)
    except Exception:
        return None
    if not raw:
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return ("", raw, "vault")
    if not isinstance(record, dict):
        return ("", raw, "vault")
    token = str(record.get("token") or "")
    if not token:
        return None
    return (str(record.get("username") or ""), token, "vault")


def _from_prefs(host):
    prefs = _prefs()
    if prefs is None or not host:
        return None
    if (getattr(prefs, "git_host", "") or "").strip() != host:
        return None
    token = (getattr(prefs, "git_token", "") or "").strip()
    if not token:
        return None
    return ((getattr(prefs, "git_username", "") or "").strip(), token,
            "preferences")


def lookup(url):
    """(username, token, source) for *url*; ("", "", "missing") if none."""
    host = host_for(url)
    for result in (_from_session(host), _from_env(), _from_vault(host),
                   _from_prefs(host)):
        if result is not None:
            return result
    return ("", "", "missing")


def status_for_host(host):
    """Source of the credentials for *host* without exposing the secret."""
    host = (host or "").strip()
    for name, result in (("session", _from_session(host)),
                         ("environment", _from_env()),
                         ("vault", _from_vault(host)),
                         ("preferences", _from_prefs(host))):
        if result is not None:
            return name
    return "missing"


def save(url, username, token):
    """Store credentials for *url*'s host; returns the store used."""
    host = host_for(url)
    if not token or not host:
        forget(url)
        return "missing"
    keyring = _keyring_module()
    stored_in_vault = False
    if keyring is not None:
        try:
            keyring.set_password(
                SERVICE, host,
                json.dumps({"username": username or "", "token": token}))
            stored_in_vault = True
        except Exception:
            stored_in_vault = False
    if stored_in_vault:
        prefs = _prefs()
        if prefs is not None and (getattr(prefs, "git_host", "") or "") \
                == host:
            prefs.git_token = ""
        return "vault"
    prefs = _prefs()
    if prefs is not None:
        prefs.git_host = host
        prefs.git_username = username or ""
        prefs.git_token = token
        return "preferences"
    _session[host] = (username or "", token)
    return "session"


def forget(url):
    """Delete credentials for *url*'s host from every store."""
    host = host_for(url)
    keyring = _keyring_module()
    if keyring is not None and host:
        try:
            keyring.delete_password(SERVICE, host)
        except Exception:
            pass
    prefs = _prefs()
    if prefs is not None and (getattr(prefs, "git_host", "") or "") == host:
        prefs.git_token = ""
    _session.pop(host, None)
