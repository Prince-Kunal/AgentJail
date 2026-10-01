# Siege

**Adaptive AI red team for AI agents — with fixes enforced by Cedar policies.**
Team Asymptotic · repo `AgentJail`

Siege attacks a deliberately vulnerable tool-using agent (ShopBot), records every
tool call it is tricked into making, writes a [Cedar](https://www.cedarpolicy.com/)
policy for each finding, and then re-runs the attacks to prove the policy blocks
them while legitimate use keeps working.

Everything runs **locally and for free** on [Ollama](https://ollama.com): no API
keys, no accounts, no cloud costs.

> The design, decisions and build plan live in
> [`Siege_Implementation_Plan.md`](Siege_Implementation_Plan.md), the project's
> source of truth.

## Status

Phase 0 (foundations) is complete: settings, the LLM layer (local Ollama + a
scripted fake for tests) and the test suite. The agent, attack loop, Cedar
enforcement and report land in Phases 1–5 (plan §9). This README is updated
whenever a phase changes how Siege is run.

## Requirements

| | |
|---|---|
| OS | macOS or Linux |
| Python | 3.12+ |
| Ollama | any recent version (developed on 0.32.5) |
| Disk | ~14 GB for the two models |
| RAM | ~8 GB free for `qwen2.5:7b`; ~10 GB for `qwen2.5:14b` (used only for Cedar generation, plan §6.5) |

## Quickstart

```bash
# 1. Get the code
git clone https://github.com/Prince-Kunal/AgentJail.git
cd AgentJail

# 2. Install Ollama (https://ollama.com/download), start it, and pull the models
ollama pull qwen2.5:7b      # attacker, target agent, labeller
ollama pull qwen2.5:14b     # second model in the Cedar generator cascade

# 3. Python environment
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

# 4. Check everything works
pytest              # 40 unit tests, no network or LLM needed (~10 s)
pytest -m live      # 3 smoke tests against your local Ollama
```

No Python 3.12? On macOS: `brew install python@3.12`. On Linux, use your
distribution's package or [python.org](https://www.python.org/downloads/).

## Configuration

Defaults need no configuration. To change a model or setting, copy
[`.env.example`](.env.example) to `.env` and edit it; real environment variables
win over `.env`. Each LLM role (`attacker`, `target`, `labeller`, `cedar`) has its
own `SIEGE_<ROLE>_*` settings, and the Cedar generator tries the models in
`SIEGE_CEDAR_MODELS` in order. Cloud providers are optional and not needed.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `pytest -m live` reports tests **skipped** | Ollama isn't running, or the model isn't pulled. Start Ollama and run `ollama pull qwen2.5:7b`. |
| `ModuleNotFoundError: No module named 'siege'` when running a script | macOS can mark the editable-install `.pth` file hidden, and Python 3.12 then skips it. Run from the repo root or prefix with `PYTHONPATH=.` (`pytest` already handles this). |
| The first LLM call is slow | Ollama is loading the model into memory; later calls are ~2 s on an Apple M4. |

## Repository layout

```
siege/            the package (config, LLM layer; agent, orchestrator, report to come)
tests/            pytest suite (unit tests by default, `-m live` for Ollama)
Siege_Implementation_Plan.md   design and build plan (source of truth)
```
