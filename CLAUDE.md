# CLAUDE.md — working agreement for this repo

This file is context for Claude Code. Read it before writing anything.

## What this is

A take-home for interface.ai: a computer-use automation system for legacy
back-office banking applications that expose no API. An LLM discovers how to
accomplish a goal by driving the real UI once; the successful run is compiled
into a typed, versioned **capability artifact**; that artifact then replays
deterministically with no model in the loop.

Evaluation weights, in their order: system design, correctness of the core
loop, robustness and error handling, human-in-the-loop escalation,
generalisation to heterogeneous surfaces, safety and data handling, code
quality, communication.

They explicitly do **not** reward feature breadth, framework name-dropping, or
scaling infrastructure. A small, correct, well-argued system is the goal.

## Non-negotiable design rules

These are the load-bearing decisions. Do not undo them, do not "improve" them,
and if a change seems to require breaking one, stop and say so instead.

1. **No LLM in the replay path.** Replay resolves stored locators, asserts
   checkpoints, and returns a typed result. If you find yourself wanting to
   call the model during replay, the answer is an escalation, not a model call.

2. **Refs are ephemeral; locators are durable.** `AXNode.ref` is valid only for
   the current observation and must never be serialised into an artifact.
   `AXLocator` is the persisted form. Never conflate them.

3. **Perception is the accessibility tree.** Not CSS selectors, not screenshot
   coordinates. Screenshots are evidence only, never perception. CSS appears
   only as a recorded fallback with a `weaker_because` justification.

4. **The policy is a gate, not a prompt.** Every action passes
   `Policy.url_allowed` / `action_allowed` / `classify` in the surface-call
   path before it reaches the browser. Never rely on prompt instructions for
   safety. Never let the agent reach `/_faults` or `subaccount/confirm`.

5. **Risk is a property of (action, target), not of the action verb.** A click
   is safe; a click on a control named "Post Transaction" is not. Risk is
   classified from the control's accessible name.

6. **The model produces a plan, never data.** It points at the node containing
   a value; the value is read from the surface. The model's claim of success is
   never trusted — the success condition is evaluated independently against the
   live surface.

7. **Three-way result contract.** `success` / `business` / `failure`, plus
   `escalated`. A "no record found" is a business outcome, not an error.
   Business outcomes and recovery rules are **declared on the artifact** at
   discovery time and human-reviewed — never inferred at replay time.

8. **Recovery is bounded and declarative.** Each `RecoveryRule` is a
   detect-then-do pair with an attempt cap. No open-ended retry, no improvising.
   An unrecognised state escalates.

9. **Redact at write time.** Sensitive values are redacted in the record
   constructor, not in a post-processing pass — the post-processing pass is
   what doesn't run when the process dies.

10. **Every recorded locator carries a written `rationale`.** A reviewer months
    later was not present when it was recorded. Where targeting is weak (a
    non-zero `ordinal`, a nameless control), the rationale must say so.

## Architecture

```
keystone/
  config.py          Settings (env) + Policy (policy.yaml). Done.
  schemas.py         The capability contract + replay result contract. Done.
  surface/
    base.py          Surface ABC, AXNode, Observation, ActionRequest. Done.
    web.py           Playwright adapter: AX extraction, act, resolve.  Done.
  evidence.py        Crash-safe JSONL evidence writer.                 Done.
  guardrails.py      Thin enforcement wrapper over Policy.             Done.
  llm.py             NIM (OpenAI-compatible) client w/ 429 backoff.    Done.
  replay/engine.py   Deterministic executor + error taxonomy.          Done.
  discovery/
    agent.py         Observe/decide/act loop.                          Done.
    compiler.py      Run trace -> CapabilityArtifact.                  Done.
    prompts.py       System prompt + action schema.                    Done.
  escalation/
    controller.py    Stuck detection, intervention requests, resume.   Done.
    console.py       Minimal operator console over WebSocket.          Done.
  cli.py             discover / replay / approve / catalog.            Done.
mockapp/             Hostile legacy frameset app + fault switchboard. Done.
tests/                                                                 Done.
policy.yaml          The reviewable guardrail contract. Done.
```

## Conventions

- Python 3.11+, **async** throughout (the live-session handoff requires the
  browser to stay alive while a websocket server runs concurrently).
- Playwright async API. Pydantic v2 for all serialised types.
- **One browser context per run, never reused.** This is the multi-tenant
  isolation control. Create at run start, destroy at run end.
- Type hints everywhere. `from __future__ import annotations` at the top.
- Docstrings explain **why**, not what. Do not write comments that restate the
  code. Do not add a docstring to a three-line helper whose name says it.
- No new dependencies without a reason stated in the commit message. In
  particular: no Postgres, no Redis, no Docker, no Celery, no LangChain. Flat
  JSON files and SQLite are sufficient and are the correct scope.

## Evidence durability requirements

- Append-only JSONL, one record per line, `flush()` + `os.fsync()` at step
  boundaries. Never buffer the whole run and write at the end.
- Two records per step: `step_started` before the action, `step_finished`
  after. An orphaned `step_started` localises a crash.
- Every record carries `schema_version` and `kind`. The reader dispatches on
  `kind` and skips unknown kinds rather than raising.
- The reader tolerates a truncated trailing line.
- One evidence directory per `run_id`, one writer per directory.
- `result.json` written to a temp file then `os.replace()`d (atomic on POSIX).
  Its presence means the run finished; its absence means crashed or running.
- Screenshots: write `.part`, then rename.

## Testing

Every phase lands with tests. Priorities, in order: the artifact schema's
validators, locator resolution and fallback ordering, guardrail decisions, the
error taxonomy (each of the three branches), and one end-to-end replay against
the mock app including an injected fault. Replay tests must not require an API
key — that is the point of replay having no model.

## Scope discipline

In scope and must be real: the artifact schema, deterministic replay with the
error taxonomy, the safety model, and the escalation/control-transfer seam.

Deliberately thin, and documented as such in REPORT.md: the operator console
(mock it, but make the handoff mechanism real), desktop surface support (design
only), multi-tenant orchestration (two mock tenants, not infrastructure).

If a request would expand scope past this, say so before building it.

## Deliverables (exact paths — they read submissions side by side)

- `/README.md` — setup, config, and the exact demo command path.
- `/REPORT.md` — seven headings, in this order: Architecture; Artifact schema;
  Determinism & error handling; Heterogeneity & multi-tenant; Escalation &
  handoff; Safety; Cuts.
- `/evidence/` — a saved artifact plus logs from a real discovery run and a
  replay run, including one replay that hits an error or exceptional state.

## The hard constraint on how we work

The brief says: *"you own everything you submit and must be able to explain and
defend any part of it in detail."*

So: build in small phases, and after each one, stop. Do not generate large
volumes of code in one pass. If I cannot explain a file, it does not ship.
