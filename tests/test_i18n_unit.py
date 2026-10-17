"""i18n — server-generated user-facing copy: per-string settings overrides + built-in locale packs.

The ticket-closed / auto-away notices and the operator/assistant author labels default to the active
SUPPORT_LOCALE pack (en|ru, see supportal.i18n); each notice stays individually overridable by its own
setting (SUPPORT_CLOSED_NOTICE / SUPPORT_AWAY_MESSAGE), which always wins over the locale default.

(The widget's own UI copy is localized client-side — a built-in ru pack in widget.js selected by the
/support/config `locale`, plus a `strings` override; that path is vanilla JS, covered by the demo smoke.)
"""
import asyncio, os, tempfile

from supportal import i18n
from supportal import settings as S
from supportal.relay import Relay
from supportal.store import Store


class _FakeTG:
    async def ensure_topic(self, title: str) -> int:
        return 1

    async def send_to_topic(self, thread_id: int, text: str) -> None:
        return None

    async def delete_topic(self, thread_id: int) -> None:
        return None

    async def close(self) -> None:
        return None


def _relay():
    path = os.path.join(tempfile.mkdtemp(prefix="supportal_i18n_"), "t.db")
    store = Store(f"sqlite:///{path}"); store.bootstrap()
    return Relay(store, _FakeTG(), "", ""), store


def _force_env_only(tmp_path):
    S._SETTINGS_FILE = str(tmp_path / "none.yaml")
    S._cache = {}; S._cache_mtime = -1.0; S._last_stat = 0.0; S._loaded_once = False


def _close(relay, store):
    return _close_row(relay, store).text


def _close_row(relay, store):
    t = store.create("u", "U")
    store.attach_thread(t.ticket_id, 1)
    asyncio.run(relay.on_operator_command(1, "/close", "op"))
    return store.changes_since("u", 0)[-1]


def test_closed_notice_default(tmp_path, monkeypatch):
    _force_env_only(tmp_path)
    monkeypatch.delenv("SUPPORT_CLOSED_NOTICE", raising=False)
    relay, store = _relay()
    assert _close(relay, store) == "This ticket was closed by the operator."


def test_closed_notice_override(tmp_path, monkeypatch):
    _force_env_only(tmp_path)
    monkeypatch.setenv("SUPPORT_CLOSED_NOTICE", "Ticket cerrado por el operador.")
    relay, store = _relay()
    assert _close(relay, store) == "Ticket cerrado por el operador."


# ── built-in locale packs (feature SUPPORT_LOCALE) ──────────────────────────────────────────

def test_i18n_defaults_english(tmp_path, monkeypatch):
    _force_env_only(tmp_path)
    monkeypatch.delenv("SUPPORT_LOCALE", raising=False)
    assert i18n.locale() == "en"
    assert i18n.t("operator") == "Operator"
    assert i18n.t("assistant") == "Assistant"
    assert i18n.t("closed_notice") == "This ticket was closed by the operator."


def test_i18n_russian_pack(tmp_path, monkeypatch):
    _force_env_only(tmp_path)
    monkeypatch.setenv("SUPPORT_LOCALE", "RU")          # case-insensitive
    assert i18n.locale() == "ru"
    assert i18n.t("operator") == "Оператор"
    assert i18n.t("assistant") == "Ассистент"
    assert i18n.t("closed_notice") == "Обращение закрыто оператором."
    assert i18n.t("away_message").startswith("Спасибо")


def test_i18n_unknown_locale_falls_back_to_english(tmp_path, monkeypatch):
    _force_env_only(tmp_path)
    monkeypatch.setenv("SUPPORT_LOCALE", "fr")          # not shipped
    assert i18n.locale() == "en"
    assert i18n.t("operator") == "Operator"


def test_i18n_unknown_key_is_empty(tmp_path, monkeypatch):
    _force_env_only(tmp_path)
    monkeypatch.setenv("SUPPORT_LOCALE", "ru")
    assert i18n.t("nope_not_a_key") == ""              # unknown key → "" (never raises)


def test_closed_notice_locale_ru(tmp_path, monkeypatch):
    """SUPPORT_LOCALE=ru (and no explicit override) → the close notice and author are Russian."""
    _force_env_only(tmp_path)
    monkeypatch.delenv("SUPPORT_CLOSED_NOTICE", raising=False)
    monkeypatch.setenv("SUPPORT_LOCALE", "ru")
    relay, store = _relay()
    row = _close_row(relay, store)
    assert row.text == "Обращение закрыто оператором."
    assert row.author == "Оператор"


def test_explicit_notice_beats_locale(tmp_path, monkeypatch):
    """An explicit SUPPORT_CLOSED_NOTICE wins even when a locale pack is active."""
    _force_env_only(tmp_path)
    monkeypatch.setenv("SUPPORT_LOCALE", "ru")
    monkeypatch.setenv("SUPPORT_CLOSED_NOTICE", "Custom close.")
    relay, store = _relay()
    assert _close(relay, store) == "Custom close."
