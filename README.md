# keystone

Computer-use automation for legacy back-office applications that expose no API.

An LLM discovers how to accomplish a goal by driving the real UI once. The
successful run is compiled into a typed, versioned **capability artifact**.
That artifact then replays deterministically, with no model in the loop, and
returns a typed result to the calling agent.

Every command below is one this project's own author actually ran against
the code in this repo, not an aspirational example.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env    # then paste your NVIDIA_API_KEY into it
```

The LLM is NVIDIA NIM's free hosted tier (OpenAI-compatible; get a key at
build.nvidia.com, no card required). Any other OpenAI-compatible endpoint
(vLLM, Ollama, a local NIM container) works by changing `LLM_BASE_URL` alone
-- `keystone/llm.py` is the stock `openai` SDK pointed at a different base URL.

**Replay never calls a model.** The entire replay/approve/catalog path below
needs no API key at all -- run it, and the test suite, with `NVIDIA_API_KEY`
unset entirely if you want to confirm that for yourself. Only `discover`
needs one.

### Config (`.env`)

| Variable | Default | What it does |
|---|---|---|
| `NVIDIA_API_KEY` | -- | Required for `discover` only. |
| `LLM_BASE_URL` | NIM's endpoint | Any OpenAI-compatible endpoint works. |
| `LLM_MODEL` | `openai/gpt-oss-20b` | See "If discovery can't reach a model" below before changing this. |
| `KEYSTONE_MAX_STEPS` | `25` | Discovery's step budget before giving up. |
| `KEYSTONE_RUN_TIMEOUT_S` | `1200` | Wall-clock budget for a discovery run. Set this high, not low -- see below. |
| `KEYSTONE_HEADLESS` | `1` | Set to `0` to watch the browser; required for `replay --attended` and the escalation demo. |
| `KEYSTONE_POLICY` | `policy.yaml` | The reviewable safety-guardrail contract. |

**Why `KEYSTONE_RUN_TIMEOUT_S=1200`, not something smaller:** measured live
against the NIM free tier, identical back-to-back calls to the *same* model
ranged from ~2s to ~48s. That's shared-queue load on the free tier, not
something a smaller prompt or a different model fixes (both were tested and
ruled out as levers -- see `REPORT.md`). Discovery is meant to run once and
be patient; replay has no LLM call on its path at all, so this timeout never
applies there.

**If discovery can't reach a model:** NIM's `/models` endpoint lists far more
models than a given free-tier key is actually entitled to *invoke* --
listing and invoke-permission are different account states there. If
`LLM_MODEL` 404s with `Function ... not found for account`, that's this, not
a broken key. `curl -H "Authorization: Bearer $NVIDIA_API_KEY"
https://integrate.api.nvidia.com/v1/models` to see the catalog, and try a
few candidates -- `keystone/llm.py`'s `complete_json(..., schema=...)` requires
`json_schema` strict-mode support, which not every listed model actually
honours even when it does respond.

## The automation target

A deliberately hostile mock of a legacy credit-union servicing console:
classic framesets, table-based layout with no `<label for>` anywhere,
meaningless class names (`class="c2"`, `name="txtMbrNo"`), no test IDs, and a
fault switchboard that injects real runtime conditions (validation errors,
permission denial, an interstitial dialog, session timeout, slow responses,
server errors) on demand.

```bash
python -m mockapp        # binds to http://127.0.0.1:8800 only
```

- `http://127.0.0.1:8800/t/pinnacle/lookup` -- member lookup. Sample member
  numbers: `12345`, `22881`, `30014` (dormant), `44120` (restricted).
- `http://127.0.0.1:8800/t/harbor/` -- a second tenant on the same product
  version: the lookup fields are reordered and the field/button captions are
  renamed, standing in for hundreds of institutions running one vendor
  product configured differently.
- `http://127.0.0.1:8800/_faults/panel` -- arm/disarm fault conditions.

## Running the tests

```bash
python -m pytest
```

Run as `python -m pytest`, not bare `pytest` -- this project isn't
pip-installed (no `[build-system]` in `pyproject.toml`, deliberately, to
avoid a packaging step this scope doesn't need), so `-m` is what puts the
repo root on `sys.path` for `import keystone` to resolve. Bare `pytest` fails
with `ModuleNotFoundError: No module named 'keystone'`.

144 tests, no API key required for any of them -- confirmed directly by
running the full suite with `.env` removed and `NVIDIA_API_KEY`/`LLM_API_KEY`
unset from the environment entirely. The suite starts the mock app itself
(reusing one already running on :8800 if it finds one) and gives every test
its own browser context.

## Demo path

Everything below assumes the mock app is running (`python -m mockapp`) and,
for step 1 only, a working `NVIDIA_API_KEY` in `.env`.

```bash
# 1. Discovery -- a real LLM-driven run against the live surface. Ends by
#    independently re-verifying the compiled artifact's success condition
#    against the live page (never trusting the model's own "done" claim),
#    then replaying it once more against a genuinely different member on a
#    fresh browser context before ever saving it.
python -m keystone discover "look up member 12345 and read their regular savings balance" \
  --entry http://127.0.0.1:8800/t/pinnacle/lookup --tenant pinnacle \
  --capability-id lookup_member_savings_balance_auto \
  --verify-param member_id=22881

# 2. That artifact is still a *draft* -- unattended replay of a draft is
#    refused by design. Replay it under supervision instead:
python -m keystone replay lookup_member_savings_balance_auto --param member_id=12345 --attended

# 3. The hand-authored, already-approved artifact: a normal successful replay.
python -m keystone replay lookup_member_savings_balance --param member_id=12345

# 4. The same artifact hitting a real business outcome -- not an error.
python -m keystone replay lookup_member_savings_balance --param member_id=99999

# 5. ...and a permission-denied business outcome, distinguished from #4 by
#    role (status vs. alert), never by scraping message text.
python -m keystone replay lookup_member_savings_balance --param member_id=44120

# 6. A replay that hits an injected fault and recovers (the message-of-the-
#    day interstitial, dismissed automatically before it can block anything).
curl -X POST http://127.0.0.1:8800/_faults/interstitial/arm
python -m keystone replay lookup_member_savings_balance --param member_id=22881

# 7. A replay that hits an injected fault and correctly does NOT recover --
#    a hard failure with the failing step, what was expected, what was
#    observed, and a real screenshot.
curl -X POST http://127.0.0.1:8800/_faults/server_error/arm
python -m keystone replay lookup_member_savings_balance --param member_id=12345
curl -X POST http://127.0.0.1:8800/_faults/clear

# 8. Promote a draft to approved. Runs the artifact N times first and
#    refuses to approve unless every run passed cleanly with every locator
#    resolving via its primary match (no fallback quietly papering over
#    drift) -- --force overrides, on the human's own authority, not the
#    tool's.
python -m keystone approve lookup_member_savings_balance_auto --by "your-name" --param member_id=12345 --runs 3

# 9. The agent-facing capability catalog, as OpenAI-style tool schemas.
python -m keystone catalog --state approved
```

Evidence for every run above lands in `evidence/<run-id>/`. Five real runs
from exactly this path are committed under `evidence/` -- see
`evidence/README.md` for which is which and why those five specifically.

### Escalation and handoff

A full real-time operator console is out of scope by design (see
`REPORT.md`'s Escalation & handoff section for what that would take); the
handoff *mechanism* is real and demonstrated end to end:

```bash
python scripts/run_escalation_demo.py
```

Runs the same artifact twice against mockapp's actual "Post Transaction"
button (already classified risky by `policy.yaml`, not a synthetic example):
unattended, it escalates and stops; attended, it pauses, a simulated human
acts directly on the *same live browser session* (bypassing this project's
own guardrail entirely, exactly as a person clicking the visible, headed
window would), and the run resumes and completes -- re-checking the paused
step's own postcondition rather than blindly continuing at the next step
(see `REPORT.md` for why that distinction matters).

## Project layout

```
keystone/
  config.py          Settings (env) + Policy (policy.yaml)
  schemas.py          The capability contract + replay result contract
  conditions.py        Shared Condition evaluator (replay checkpoints, discovery's success check)
  evidence.py          Crash-safe JSONL evidence writer/reader
  guardrails.py        Policy enforcement wrapper -- the only way to reach a Surface
  reliability.py        N-run replay + locator-strength scoring, feeding `approve`
  llm.py               NIM (OpenAI-compatible) client; the only file allowed to import openai
  surface/
    base.py             The Surface ABC -- perception/action seam, surface-agnostic
    web.py              Playwright adapter: AX-tree perception, act, resolve
  replay/engine.py       Deterministic executor + error taxonomy, no LLM
  discovery/
    agent.py             Observe/decide/act loop + the discover() pipeline
    compiler.py          Trace -> CapabilityArtifact, pure transformation
    prompts.py           System prompt + strict JSON action schema
  escalation/
    controller.py        Pause/handoff/resume seam over a live surface
    console.py           Minimal (real) WebSocket operator console
  cli.py               discover / replay / approve / catalog
mockapp/               The hostile legacy-app stand-in + fault switchboard
scripts/               Manual verification tools (dump_ax, run_replay, run_discovery, run_escalation_demo)
artifacts/             Saved capability artifacts (draft and approved)
policy.yaml            The reviewable safety-guardrail contract
tests/                 110 tests, no API key required
```

## Design

`REPORT.md` covers the architecture, the artifact schema, determinism and
error handling, heterogeneity and multi-tenant reuse, escalation and
handoff, the safety model, and what was deliberately cut and why.
