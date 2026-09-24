# grip

Computer-use automation for legacy back-office applications that expose no API.

An LLM discovers how to accomplish a goal by driving the real UI once. The
successful run is compiled into a typed, versioned **capability artifact**.
That artifact then replays deterministically, with no model in the loop, and
returns a typed result to the calling agent.

> Status: in progress. See `BUILD_PLAN.md` for what is built and what is next.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env    # add your NVIDIA_API_KEY
```

The LLM is NVIDIA NIM's free hosted tier, which is OpenAI-compatible. Any other
OpenAI-compatible endpoint works by changing `LLM_BASE_URL`.

Replay never calls a model, so **the replay demo path needs no API key**.

## The automation target

A deliberately hostile mock of a legacy credit-union servicing console:
framesets, table-based layout with no `<label for>`, meaningless class names,
no test IDs, and a fault switchboard that injects real runtime conditions.

```bash
python -m mockapp        # http://127.0.0.1:8800/t/pinnacle/
```

Two tenants run the same product version. `harbor` reorders the lookup fields
and renames one caption, standing in for the real environment's many
institutions on one vendor product.

Fault switchboard: <http://127.0.0.1:8800/_faults/panel>

## Demo path

```bash
# 1. discovery — a real LLM run against the live surface
python -m grip discover "look up member 12345 and read their regular savings balance" \
  --entry http://127.0.0.1:8800/t/pinnacle/lookup --tenant pinnacle

# 2. deterministic replay with a different input, no model involved
python -m grip replay lookup_member_savings_balance --member_id 22881

# 3. a replay that hits an expected business outcome
python -m grip replay lookup_member_savings_balance --member_id 99999

# 4. a replay that hits an injected runtime fault and recovers
curl -XPOST localhost:8800/_faults/interstitial/arm
python -m grip replay lookup_member_savings_balance --member_id 12345
```

Evidence for each run lands in `evidence/<run-id>/`.

## Design

`REPORT.md` covers the architecture, the artifact schema, determinism and error
handling, heterogeneity and multi-tenant reuse, escalation and handoff, the
safety model, and what was deliberately cut.
