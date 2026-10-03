# Siege — demo script & rehearsal checklist (plan §8, P8.2)

**The pitch (say this first):** "Siege is an adaptive AI red team. It attacks a
deliberately vulnerable tool-using agent, records the breach, writes a Cedar
policy that fixes it, and *re-runs the same attack to prove the fix holds* — the
model is still fooled, but Cedar says no. Two independent layers: a sandbox
contains the agent, Cedar denies authorized-but-wrong actions."

Target length: **~5 minutes**. Have two terminals open.

---

## Pre-flight (do this before the audience is watching)

- [ ] `ollama list` shows `qwen2.5:14b` and `qwen2.5:7b`; `ollama ps` is warm (do one throwaway call so the first live call isn't a cold load).
- [ ] `docker info` succeeds (Docker Desktop running).
- [ ] Clean checkout on `main`, `pip install -e .` done, `pytest -q` green.
- [ ] `siege verify demo` prints **VERIFIED** (the offline backup works — see below).
- [ ] Decide the path: **A. live** (needs Ollama) or **B. offline** (judge path, no LLM). If the room's machine is weak or offline, use B.

---

## Path A — live (the full story)

1. **Start the sandbox** (terminal 1):
   ```sh
   SIEGE_TARGET_MODEL=qwen2.5:7b ./sandbox/run.sh
   ./sandbox/check.sh          # show the hardening: non-root, read-only fs, isolated
   ```
   Say: *"Only the target runs in here — read-only, non-root, all caps dropped. The attacker and orchestrator stay on the host."*

2. **Run the whole flow against the sandbox** (terminal 2, use the canary run.sh printed):
   ```sh
   SIEGE_CANARY=<printed> siege demo --no-launch --goal G2 --target-model qwen2.5:7b
   ```
   Narrate as it goes: the attacker **adapts over turns** → **BREACH** (the agent
   leaked internal notes) → **FIX** (a model wrote a Cedar `forbid`, validated +
   decision-tested) → **RERUN** → **BLOCKED** ("still fooled, Cedar still said no")
   → **happy path OK** (normal use still works) → **report written**.

3. **Open the report:** `open runs/<id>/report.html` — before/after per finding,
   the exact tool call as evidence, the policy and who wrote it, decision tests.

> If the attacker stalls (the 7b sometimes wanders), don't wait — Ctrl-C and switch
> to Path B. The story is identical; it just skips the live generation.

## Path B — offline judge path (no LLM, seconds)

```sh
siege verify demo        # VERIFIED: breaches blocked, happy path OK (deterministic)
open demo/report.html
```
Say: *"No GPU, no Ollama, no network — this replays the recorded attack through the
exact Cedar policies and shows the same before/after in seconds. Anyone can
reproduce it on a laptop."*

---

## Rehearsal checklist (Phase 8 exit)

- [ ] Full **Path A** rehearsed end to end **twice** (different goals, e.g. G2 then G1).
- [ ] Timed: under ~5 min including narration.
- [ ] **Path B** confirmed to work **offline** (turn off Wi-Fi, stop Ollama, run `siege verify demo`).
- [ ] `docker rm -f siege-target` cleanup rehearsed.
- [ ] Fallback plan agreed: if live breaks, switch to Path B without missing a beat.
- [ ] One person drives, one narrates.
