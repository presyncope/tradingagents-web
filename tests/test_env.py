"""The web .env can hold its own settings and name the TradingAgents .env with the keys."""

from __future__ import annotations

import pytest

from tradingagents_web import env

KEYS = ("TRADINGAGENTS_WEB_ENV_FILE", "TRADINGAGENTS_WEB_TZ", "TA_TEST_FAKE_KEY")


@pytest.fixture
def clean(monkeypatch, tmp_path):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)    # restored (removed) after the test
    monkeypatch.setattr(env, "_loaded", False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_the_local_env_file_can_point_at_the_key_file(clean):
    keys = clean / "keys.env"
    keys.write_text("TA_TEST_FAKE_KEY=from-keys\nTRADINGAGENTS_WEB_TZ=Europe/Paris\n")
    (clean / ".env").write_text(f"TRADINGAGENTS_WEB_TZ=Asia/Seoul\nTRADINGAGENTS_WEB_ENV_FILE={keys}\n")

    loaded = env.load_env()

    assert [p.name for p in loaded] == [".env", "keys.env"]
    assert env.os.environ["TA_TEST_FAKE_KEY"] == "from-keys"
    assert env.os.environ["TRADINGAGENTS_WEB_TZ"] == "Asia/Seoul"      # the first file wins
    assert env.load_env() == []                                        # once per process


def test_a_file_named_by_the_shell_is_read_first(clean, monkeypatch):
    keys = clean / "keys.env"
    keys.write_text("TRADINGAGENTS_WEB_TZ=Europe/Paris\n")
    (clean / ".env").write_text("TRADINGAGENTS_WEB_TZ=Asia/Seoul\n")
    monkeypatch.setenv("TRADINGAGENTS_WEB_ENV_FILE", str(keys))

    assert [p.name for p in env.load_env()] == ["keys.env", ".env"]
    assert env.os.environ["TRADINGAGENTS_WEB_TZ"] == "Europe/Paris"


def test_no_files_is_fine(clean):
    assert env.load_env() == []
