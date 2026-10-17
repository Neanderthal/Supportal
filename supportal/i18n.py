"""Built-in locale packs for the few user-facing strings the RELAY itself generates.

Localization has two surfaces that stay independent:

* the **widget's** UI copy is localized client-side — the widget ships built-in `en`/`ru` tables and
  picks one from the `locale` field of `/support/config` (a product may still override individual keys
  via `strings`). That path never touches this module.
* the **server**-generated lines the user SEES — the operator/assistant author labels and the
  closed / away notices — are localized here, selected by the ``SUPPORT_LOCALE`` setting (default ``en``).

Each notice also remains individually overridable by its own setting (``SUPPORT_CLOSED_NOTICE`` /
``SUPPORT_AWAY_MESSAGE``): the locale only changes the DEFAULT, an explicit setting always wins.
Fail-soft: an unknown / empty ``SUPPORT_LOCALE`` falls back to English, and a key missing from a
non-English pack falls back to its English value — the service never breaks on a bad locale.

Read through :func:`t` at call time (not frozen at import) so a live ``SUPPORT_LOCALE`` change in
``settings.yaml`` applies on the next request, like every other setting.
"""
from __future__ import annotations

from .flags import Flags
from .settings import get

DEFAULT = "en"

# key → text, per locale. Keys are shared across locales; a key absent from a non-"en" pack falls back
# to "en" (see `t`). These are DEFAULTS — SUPPORT_CLOSED_NOTICE / SUPPORT_AWAY_MESSAGE override per-string.
LOCALES: dict[str, dict[str, str]] = {
    "en": {
        "operator": "Operator",
        "assistant": "Assistant",
        "closed_notice": "This ticket was closed by the operator.",
        "away_message": "Thanks for reaching out! We're away right now and will reply during business hours.",
    },
    "ru": {
        "operator": "Оператор",
        "assistant": "Ассистент",
        "closed_notice": "Обращение закрыто оператором.",
        "away_message": "Спасибо за обращение! Сейчас мы офлайн и ответим в рабочее время.",
    },
}


def locale() -> str:
    """The active locale code from SUPPORT_LOCALE (settings → env → default), normalized and validated.
    Unknown / empty → "en" (fail-soft)."""
    code = str(get(Flags.LOCALE, DEFAULT) or DEFAULT).strip().lower()
    return code if code in LOCALES else DEFAULT


def t(key: str) -> str:
    """The string for `key` in the active locale, falling back to the English pack then empty."""
    pack = LOCALES.get(locale(), LOCALES[DEFAULT])
    return pack.get(key) or LOCALES[DEFAULT].get(key, "")
