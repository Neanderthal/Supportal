"""Config.from_env — env → typed Config, defaults when unset, int-parse fallback. Offline, no network."""
from supportal.config import Config

_INT_KEYS = ("SUPPORT_TG_GROUP_ID", "SUPPORT_HTTP_PORT", "SUPPORT_OUTBOX_RETRY_INTERVAL",
             "SUPPORT_OUTBOX_TTL", "SUPPORT_OUTBOX_MAX_ATTEMPTS", "SUPPORT_OUTBOX_BACKOFF_BASE",
             "SUPPORT_OUTBOX_BACKOFF_CAP")
_STR_KEYS = ("SUPPORT_TG_BOT_TOKEN", "SUPPORT_TG_PROXY", "SUPPORT_PRODUCT_CALLBACK_URL",
             "SUPPORT_CALLBACK_SECRET", "SUPPORT_INGRESS_SECRET", "SUPPORT_WIDGET_SECRET",
             "SUPPORT_ALLOWED_ORIGIN", "SUPPORT_DB_URL")


def _clear(monkeypatch) -> None:
    for k in _INT_KEYS + _STR_KEYS:
        monkeypatch.delenv(k, raising=False)


def test_from_env_reads_values(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SUPPORT_TG_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("SUPPORT_TG_GROUP_ID", "-1001")
    monkeypatch.setenv("SUPPORT_HTTP_PORT", "9000")
    monkeypatch.setenv("SUPPORT_WIDGET_SECRET", "wsec")
    cfg = Config.from_env()
    assert cfg.tg_bot_token == "123:abc"
    assert cfg.tg_group_id == -1001          # negative chat_id parsed
    assert cfg.http_port == 9000
    assert cfg.widget_secret == "wsec"


def test_from_env_defaults_when_unset(monkeypatch):
    _clear(monkeypatch)
    cfg = Config.from_env()
    assert cfg.tg_bot_token == ""                        # _env string default
    assert cfg.tg_group_id == 0                          # _env_int empty → default
    assert cfg.db_url == "sqlite:///./supportal.db"
    assert cfg.http_port == 8500
    assert cfg.outbox_ttl == 86400 and cfg.outbox_max_attempts == 12


def test_env_int_invalid_falls_back_to_default(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SUPPORT_TG_GROUP_ID", "not-an-int")
    monkeypatch.setenv("SUPPORT_HTTP_PORT", "")          # empty → default branch
    cfg = Config.from_env()
    assert cfg.tg_group_id == 0                          # ValueError → default
    assert cfg.http_port == 8500                         # empty → default
