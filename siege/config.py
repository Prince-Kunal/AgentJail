"""Env-driven settings (plan §2, §9.4).

Every setting is read from the environment, with values from a local `.env`
file filling in anything not already set. `.env.example` lists every key.
Model names live here and nowhere else.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROVIDERS = ("anthropic", "openai", "ollama", "fake")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
ROLES = ("attacker", "target", "labeller", "cedar")

# Defaults from plan §2 and §9.4: every role runs on local Ollama with one
# shared model, so anyone can clone and run Siege with no paid API key.
# Cloud providers are opt-in via .env.
DEFAULT_OLLAMA_MODEL = "qwen2.5:7b"

_ROLE_DEFAULTS: dict[str, dict[str, str]] = {
    "attacker": {"provider": "ollama", "model": DEFAULT_OLLAMA_MODEL},
    "target": {"provider": "ollama", "model": DEFAULT_OLLAMA_MODEL, "temperature": "0"},
    "labeller": {"provider": "ollama", "model": DEFAULT_OLLAMA_MODEL, "temperature": "0"},
    "cedar": {"provider": "ollama", "model": DEFAULT_OLLAMA_MODEL, "temperature": "0"},
}


@dataclass(frozen=True)
class RoleConfig:
    """Which provider and model one LLM role uses, and how it is called.

    `model` may be empty when no default exists. Settings still load, so
    unrelated code keeps working; `require_model()` fails when the role is used.
    """

    role: str
    provider: str
    model: str
    temperature: float | None = None
    effort: str | None = None
    max_tokens: int = 16000

    def require_model(self) -> str:
        if not self.model:
            raise ValueError(f"no model set for the {self.role} role: set SIEGE_{self.role.upper()}_MODEL")
        return self.model


@dataclass(frozen=True)
class Settings:
    attacker: RoleConfig
    target: RoleConfig
    labeller: RoleConfig
    cedar: RoleConfig
    max_turns: int
    attacker_user: str
    target_url: str
    runs_dir: Path
    db_path: Path
    ollama_host: str

    def role(self, name: str) -> RoleConfig:
        if name not in ROLES:
            raise ValueError(f"unknown LLM role {name!r}; expected one of {ROLES}")
        return getattr(self, name)


def load_dotenv(path: str | Path = ".env") -> None:
    """Load KEY=VALUE lines into os.environ without overriding existing values."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _env(key: str, default: str | None = None) -> str | None:
    value = os.environ.get(key)
    return value if value not in (None, "") else default


def _role_config(role: str) -> RoleConfig:
    defaults = _ROLE_DEFAULTS[role]
    prefix = f"SIEGE_{role.upper()}_"

    provider = _env(prefix + "PROVIDER", defaults["provider"])
    if provider not in PROVIDERS:
        raise ValueError(f"{prefix}PROVIDER={provider!r}; expected one of {PROVIDERS}")

    # "none" explicitly turns a parameter off; blank/unset falls back to the default.
    effort = _env(prefix + "EFFORT", defaults.get("effort"))
    effort = None if effort == "none" else effort
    if effort is not None and effort not in EFFORTS:
        raise ValueError(f"{prefix}EFFORT={effort!r}; expected one of {EFFORTS} or 'none'")

    temperature = _env(prefix + "TEMPERATURE", defaults.get("temperature"))
    temperature = None if temperature == "none" else temperature
    return RoleConfig(
        role=role,
        provider=provider,
        model=_env(prefix + "MODEL", defaults["model"]) or "",
        temperature=float(temperature) if temperature is not None else None,
        effort=effort,
        max_tokens=int(_env(prefix + "MAX_TOKENS", "16000")),
    )


def load_settings() -> Settings:
    """Build Settings from the environment (after loading `.env`)."""
    load_dotenv(_env("SIEGE_ENV_FILE", ".env"))
    runs_dir = Path(_env("SIEGE_RUNS_DIR", "runs"))
    return Settings(
        attacker=_role_config("attacker"),
        target=_role_config("target"),
        labeller=_role_config("labeller"),
        cedar=_role_config("cedar"),
        max_turns=int(_env("SIEGE_MAX_TURNS", "10")),
        attacker_user=_env("SIEGE_ATTACKER_USER", "alice"),
        target_url=_env("SIEGE_TARGET_URL", "http://127.0.0.1:8100"),
        runs_dir=runs_dir,
        db_path=Path(_env("SIEGE_DB_PATH", str(runs_dir / "siege.db"))),
        ollama_host=_env("OLLAMA_HOST", "http://localhost:11434"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Tests call `get_settings.cache_clear()` after changing env."""
    return load_settings()
