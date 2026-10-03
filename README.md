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

Phases 0–8 are complete: the LLM layer and settings, the vulnerable target agent
(ShopBot), the deterministic evaluator and SQLite store, the adaptive PAIR-style
attack loop, Cedar generation + enforcement, the rerun that **proves each fix**,
the HTML report, a hardened Docker sandbox, and an offline judge path. `siege demo`
runs the whole loop; `siege verify demo` reproduces it with **no LLM**. This README
is updated whenever a phase changes how Siege is run.

## Requirements

| | |
|---|---|
| OS | macOS or Linux |
| Python | 3.12+ |
| Ollama | any recent version (developed on 0.32.5) |
| Disk | ~14 GB for the two models |
| RAM | ~10 GB free for `qwen2.5:14b`, the primary model (only one model is loaded at a time) |

## Quickstart

```bash
# 1. Get the code
git clone https://github.com/Prince-Kunal/AgentJail.git
cd AgentJail

# 2. Install Ollama (https://ollama.com/download), start it, and pull the models
ollama pull qwen2.5:14b     # primary model: attacker, target agent, labeller, Cedar generator
ollama pull qwen2.5:7b      # fallback in the Cedar generator cascade (plan §6.5)

# 3. Python environment
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

# 4. Check everything works
pytest              # 268 unit tests, no network or LLM needed (~7 s)
pytest -m live      # 3 smoke tests against your local Ollama
```

## Talk to the target by hand

ShopBot is the deliberately vulnerable agent Siege attacks. You can chat with it
directly:

```bash
# Start the target (in one terminal)
uvicorn siege.target.app:app --port 8100

# Talk to it as a customer (in another)
python -m siege.scripts.chat --user alice
python -m siege.scripts.chat --user alice -m "refund order 5521"
```

Each tool call the agent makes is printed before its reply; `/log` dumps the
tool log, `/quit` exits.

## Run an automated attack

`siege run` launches the target for you, drives the adaptive attacker against it,
and stores every turn:

```bash
# Attack G1 (direct unauthorized refund), printing each turn live
siege run --goal G1

# The capable 14b resists; point the launched target at the weaker model to
# see a breach quickly (the attacker still runs on the default model):
siege run --goal G1 --target-model qwen2.5:7b

siege show <run_id>      # replay a stored run: attempts, labels, findings
```

`siege run` with no `--goal` attacks every goal in `goals.yaml`. Use `--no-launch`
to attack a target you started yourself.

## The full loop: attack, fix, prove the fix

`siege demo` runs the whole story against one target — attack → generate a Cedar
fix → re-run the attack with the fix enforced → render the report:

```bash
siege demo --goal G1 --target-model qwen2.5:7b
```

It prints the breach, the model-written Cedar policy (validated and decision-tested),
the rerun outcome (`BLOCKED` — *still fooled, Cedar still said no*), the happy-path
result, and writes `runs/<id>/report.html`. The steps are also separate commands:
`siege fix <run_id>`, `siege rerun <run_id>`, `siege report <run_id>`.

### In a hardened sandbox

Run the target inside Docker (non-root, read-only root filesystem, all capabilities
dropped, restricted egress) and drive the same demo against it:

```bash
SIEGE_TARGET_MODEL=qwen2.5:7b ./sandbox/run.sh    # builds + starts it, prints the canary
./sandbox/check.sh                                 # verify the isolation
SIEGE_CANARY=<printed> siege demo --no-launch      # attack the sandboxed target
```

See [`sandbox/README.md`](sandbox/README.md) for the hardening details and the
Firecracker microVM path.

## Reproduce in seconds without an LLM (the judge path)

A recorded run lives in [`demo/`](demo/). `siege verify demo` replays its breaches
and the happy path through the exact Cedar policies — deterministically, with **no
Ollama, no network, no running target** — and re-renders the report:

```bash
siege verify demo        # VERIFIED: recorded breaches blocked, happy path OK
open demo/report.html
```

This is the backup for a live demo and the fastest way to confirm "attack blocked,
normal use still works" on any laptop. The demo script is in [`demo/SCRIPT.md`](demo/SCRIPT.md).

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
| `pytest -m live` reports tests **skipped** | Ollama isn't running, or the model isn't pulled. Start Ollama and run `ollama pull qwen2.5:14b`. |
| `ModuleNotFoundError: No module named 'siege'` when running a script | macOS can mark the editable-install `.pth` file hidden, and Python 3.12 then skips it. Run from the repo root or prefix with `PYTHONPATH=.` (`pytest` already handles this). |
| The first LLM call is slow | Ollama is loading the model into memory; later calls are ~3–4 s on an Apple M4. |

## Repository layout

```
siege/            the package (config, LLM layer; agent, orchestrator, report to come)
tests/            pytest suite (unit tests by default, `-m live` for Ollama)
Siege_Implementation_Plan.md   design and build plan (source of truth)
```
