import os

import pytest

from siege.config import get_settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    """Every test sees default settings: no SIEGE_*/OLLAMA_* env vars and no .env file."""
    for key in list(os.environ):
        if key.startswith(("SIEGE_", "OLLAMA_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("SIEGE_ENV_FILE", os.devnull)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
