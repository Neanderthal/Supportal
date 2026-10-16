"""Runtime settings for feature flags: ``settings.yaml`` → environment → code default.

Features are gated by flags read through these getters, so a flag can be flipped by editing
``settings.yaml`` **live** (re-read by mtime, no restart) — this is the "settings" surface.
Fail-soft by design: a missing file, missing PyYAML, or a parse error falls back to the
environment and then the code default, so the service never depends on the file existing.

    priority:  settings.yaml  >  os.environ  >  default

Keys in ``settings.yaml`` are the env-var names, e.g.::

    SUPPORT_RATE_LIMIT: on
    SUPPORT_RATE_LIMIT_MAX: 20

Path is ``settings.yaml`` in the working directory, overridable via ``SUPPORT_SETTINGS_FILE``.
"""
from __future__ import annotations

import os
import time

_SETTINGS_FILE = os.environ.get("SUPPORT_SETTINGS_FILE", "settings.yaml")
_STAT_TTL = 2.0           # throttle os.stat() to at most once per this many seconds

_cache: dict = {}
_cache_mtime: float = -1.0
_last_stat: float = 0.0
_loaded_once = False


def _load() -> dict:
    """Parsed settings dict, re-read only when settings.yaml's mtime changes. Fail-soft → {}."""
    global _cache, _cache_mtime, _last_stat, _loaded_once
    now = time.time()
    if _loaded_once and (now - _last_stat) < _STAT_TTL:
        return _cache
    _last_stat = now
    _loaded_once = True
    try:
        mtime = os.path.getmtime(_SETTINGS_FILE)
    except OSError:
        _cache, _cache_mtime = {}, -1.0
        return _cache
    if mtime == _cache_mtime:
        return _cache
    try:
        import yaml
        with open(_SETTINGS_FILE, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        _cache = data if isinstance(data, dict) else {}
    except Exception:
        _cache = {}          # broken file / no PyYAML → ignore the file, use env/default
    _cache_mtime = mtime
    return _cache


def get(name: str, default=None):
    """Raw value: settings.yaml → env → default."""
    data = _load()
    if name in data and data[name] is not None:
        return data[name]
    env = os.environ.get(name)
    return env if env is not None else default


def get_bool(name: str, default: bool = False) -> bool:
    v = get(name, None)
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "on", "yes")


def get_int(name: str, default: int = 0) -> int:
    v = get(name, None)
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def get_float(name: str, default: float = 0.0) -> float:
    v = get(name, None)
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default
