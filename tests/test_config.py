import os

import pytest

from siege.config import DEFAULT_CEDAR_MODELS, ROLES, RoleConfig, get_settings, load_dotenv, load_settings


def test_defaults_run_everything_on_local_ollama():
    s = load_settings()
    for role in ROLES:
        assert s.role(role).provider == "ollama"
    assert s.attacker.model == s.target.model == s.labeller.model == "qwen2.5:14b"
    assert s.target.temperature == 0.0 and s.labeller.temperature == 0.0 and s.cedar.temperature == 0.0
    assert s.attacker.temperature is None
    assert s.cedar_models == DEFAULT_CEDAR_MODELS == ("qwen2.5:14b", "qwen2.5:7b")
    assert s.cedar.model == s.cedar_models[0]
    assert s.max_turns == 10 and s.attacker_user == "alice"
    assert s.ollama_host == "http://localhost:11434"
    assert str(s.db_path) == os.path.join("runs", "siege.db")


def test_env_overrides_role_settings(monkeypatch):
    monkeypatch.setenv("SIEGE_TARGET_MODEL", "other:3b")
    monkeypatch.setenv("SIEGE_TARGET_TEMPERATURE", "0.5")
    monkeypatch.setenv("SIEGE_ATTACKER_PROVIDER", "fake")
    s = load_settings()
    assert s.target.model == "other:3b" and s.target.temperature == 0.5
    assert s.attacker.provider == "fake"


def test_none_switches_a_parameter_off(monkeypatch):
    monkeypatch.setenv("SIEGE_TARGET_TEMPERATURE", "none")
    monkeypatch.setenv("SIEGE_CEDAR_EFFORT", "none")
    s = load_settings()
    assert s.target.temperature is None and s.cedar.effort is None


def test_blank_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("SIEGE_TARGET_MODEL", "")
    assert load_settings().target.model == "qwen2.5:14b"


@pytest.mark.parametrize("key, value", [("SIEGE_CEDAR_PROVIDER", "openrouter"), ("SIEGE_ATTACKER_EFFORT", "extreme")])
def test_invalid_values_raise(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match=key):
        load_settings()


def test_dotenv_fills_gaps_but_env_wins(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('# comment\nSIEGE_MAX_TURNS=7\nSIEGE_TARGET_MODEL="from-dotenv"\nnot a pair\n')
    monkeypatch.setenv("SIEGE_ENV_FILE", str(env_file))
    monkeypatch.setenv("SIEGE_TARGET_MODEL", "from-env")
    s = load_settings()
    assert s.max_turns == 7
    assert s.target.model == "from-env"


def test_load_dotenv_ignores_missing_file(tmp_path):
    load_dotenv(tmp_path / "does-not-exist")  # no error


@pytest.mark.parametrize(
    "env, expected",
    [
        ({}, ("qwen2.5:14b", "qwen2.5:7b")),
        ({"SIEGE_CEDAR_MODEL": "qwen2.5:7b"}, ("qwen2.5:7b",)),
        ({"SIEGE_CEDAR_MODELS": " a, b ,c ", "SIEGE_CEDAR_MODEL": "ignored"}, ("a", "b", "c")),
    ],
)
def test_cedar_cascade(monkeypatch, env, expected):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    s = load_settings()
    assert s.cedar_models == expected
    assert s.cedar.model == expected[0]


def test_cedar_cascade_rejects_empty_list(monkeypatch):
    monkeypatch.setenv("SIEGE_CEDAR_MODELS", " , ")
    with pytest.raises(ValueError, match="lists no models"):
        load_settings()


def test_require_model_and_unknown_role():
    s = load_settings()
    assert s.attacker.require_model() == "qwen2.5:14b"
    with pytest.raises(ValueError, match="SIEGE_ATTACKER_MODEL"):
        RoleConfig(role="attacker", provider="ollama", model="").require_model()
    with pytest.raises(ValueError, match="unknown LLM role"):
        s.role("judge")


def test_get_settings_is_cached_until_cleared(monkeypatch):
    first = get_settings()
    monkeypatch.setenv("SIEGE_MAX_TURNS", "3")
    assert get_settings() is first
    get_settings.cache_clear()
    assert get_settings().max_turns == 3
