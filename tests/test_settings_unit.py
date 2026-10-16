"""settings.py — приоритет settings.yaml → env → дефолт, fail-soft, парсинг bool/int, hot-reload."""
import os

from supportal import settings as S


def _reset(path) -> None:
    """Указать стор на конкретный файл и сбросить кэш модуля (так тесты не зависят от порядка)."""
    S._SETTINGS_FILE = str(path)
    S._cache = {}
    S._cache_mtime = -1.0
    S._last_stat = 0.0
    S._loaded_once = False


def test_file_over_env_over_default(tmp_path, monkeypatch):
    f = tmp_path / "settings.yaml"
    f.write_text("SUPPORT_RATE_LIMIT: on\nSUPPORT_RATE_LIMIT_MAX: 5\n", encoding="utf-8")
    _reset(f)
    monkeypatch.setenv("SUPPORT_RATE_LIMIT", "off")        # файл должен победить env
    assert S.get_bool("SUPPORT_RATE_LIMIT", False) is True
    assert S.get_int("SUPPORT_RATE_LIMIT_MAX", 20) == 5
    monkeypatch.setenv("SUPPORT_ONLY_ENV", "on")           # ключ только в env
    assert S.get_bool("SUPPORT_ONLY_ENV", False) is True
    assert S.get_int("SUPPORT_ABSENT", 7) == 7             # нигде → дефолт
    assert S.get_bool("SUPPORT_ABSENT_B", True) is True


def test_missing_file_is_failsoft(tmp_path, monkeypatch):
    _reset(tmp_path / "nope.yaml")
    monkeypatch.setenv("SUPPORT_RATE_LIMIT", "on")
    assert S.get_bool("SUPPORT_RATE_LIMIT", False) is True   # нет файла → env, без падения
    _reset(tmp_path / "nope.yaml")
    monkeypatch.delenv("SUPPORT_RATE_LIMIT", raising=False)
    assert S.get_bool("SUPPORT_RATE_LIMIT", False) is False  # нет файла и env → дефолт


def test_bool_parsing(tmp_path):
    f = tmp_path / "s.yaml"
    f.write_text("A: true\nB: 'on'\nC: 1\nD: false\nE: nope\n", encoding="utf-8")
    _reset(f)
    assert S.get_bool("A") is True
    assert S.get_bool("B") is True
    assert S.get_bool("C") is True
    assert S.get_bool("D") is False
    assert S.get_bool("E") is False


def test_broken_yaml_is_failsoft(tmp_path, monkeypatch):
    f = tmp_path / "broken.yaml"
    f.write_text("this: : : not valid\n  - [", encoding="utf-8")
    _reset(f)
    monkeypatch.setenv("SUPPORT_RATE_LIMIT", "on")
    assert S.get_bool("SUPPORT_RATE_LIMIT", False) is True   # мусор → игнор файла, env/дефолт


def test_hot_reload_on_mtime_change(tmp_path):
    f = tmp_path / "settings.yaml"
    f.write_text("SUPPORT_RATE_LIMIT: off\n", encoding="utf-8")
    _reset(f)
    assert S.get_bool("SUPPORT_RATE_LIMIT", False) is False
    f.write_text("SUPPORT_RATE_LIMIT: on\n", encoding="utf-8")
    st = os.stat(str(f))
    os.utime(str(f), (st.st_mtime + 10, st.st_mtime + 10))   # гарантируем смену mtime
    S._last_stat = 0.0                                        # обойти throttle стата
    assert S.get_bool("SUPPORT_RATE_LIMIT", False) is True    # перечиталось живьём
