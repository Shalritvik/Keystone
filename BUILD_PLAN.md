# Build plan

Nine phases. Each is one Claude Code session, ends with something runnable, and
has a checkpoint you must be able to answer out loud before moving on. If you
cannot answer the checkpoint, re-read the code rather than continuing — the
interview will find that gap.

Do not let a session run past its phase. Long unsupervised generations are how
you end up with a repo you cannot defend.

---

## Phase 0 — setup (20 min, no Claude Code needed)

```bash
git init && git branch -M main
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env     # then paste your NVIDIA_API_KEY into it
python -m mockapp        # http://127.0.0.1:8800/t/pinnacle/
```

Get the key free at build.nvidia.com — no card required. Open the app and click
around: the lookup form, member 12345, member 99999 (not found), member 44120
(restricted), and the fault panel at `/_faults/panel`. **Do this before writing
any code.** You need to have seen the surface you are automating.

Then create the public repo (`gh repo create keystone --public --source=. `) and
push. Commit at the end of every phase.

---

## Phase 1 — the web surface adapter

The single hardest piece. Budget the most time here.

> Read CLAUDE.md. Implement `keystone/surface/web.py`: a `PlaywrightSurface` that
> implements the `Surface` ABC in `keystone/surface/base.py`.
>
> Perception: walk every frame in the page, and for each frame compute an
> accessibility view of its controls and named content. Use Chrome DevTools
> Protocol `Accessibility.getFullAXTree` where you can get a per-frame CDP
> session; fall back to an injected in-page script that computes role and
> accessible name itself. Whichever path produced a node, record it.
>
> The accessible-name computation must handle, in priority order:
> aria-labelledby, aria-label, `<label for>`, a wrapping `<label>`, the `value`
> attribute for submit/button inputs, `alt`, `title`, placeholder, text
> content — and, critically, **the text of the adjacent table cell to the
> left**, because the target app labels every field that way and has no
> `<label for>` anywhere. Explain that last heuristic in a docstring.
>
> Assign each node an ephemeral `ref` and tag the live element so you can act
> on it. Refs must never be persisted.
>
> Implement `act` for navigate/click/type/select/wait/read, and `resolve` for
> turning a stored `AXLocator` into a live ref — primary match first (role +
> accessible name + frame + ancestor), then each fallback in order, returning
> which strategy matched.
>
> One browser context per surface instance; close it in `close()`.
>
> Then write `scripts/dump_ax.py` that opens a URL and prints the observation,
> so I can eyeball what the tree looks like.

Verify:

```bash
python scripts/dump_ax.py http://127.0.0.1:8800/t/pinnacle/lookup
python scripts/dump_ax.py http://127.0.0.1:8800/t/harbor/lookup
```

You should see the member field found with the name `Member #:` on pinnacle and
`Account Number:` on harbor, each scoped to `contentFrame`, and the nav frame's
"Search" link as a *separate* node from the content frame's "Search" button.

**Checkpoint:** why is the adjacent-table-cell heuristic necessary, and what
does it get wrong? What happens when two controls in different frames share an
accessible name?

---

## Phase 2 — evidence

Small, and everything downstream depends on it.

> Implement `keystone/evidence.py` per the durability requirements in CLAUDE.md:
> crash-safe append-only JSONL, `step_started`/`step_finished` pairs, fsync at
> step boundaries, `schema_version` and `kind` on every record, a reader that
> tolerates a truncated trailing line and skips unknown kinds, atomic
> `result.json` via `os.replace`, screenshots written `.part` then renamed, and
> redaction applied in the record constructor.
>
> Include a test that kills the writer mid-run and asserts the reader still
> returns every completed record plus the orphaned `step_started`.

**Checkpoint:** why redact in the constructor instead of on the way out? Why
two records per step?

---

## Phase 3 — guardrails and the LLM client

> Implement `keystone/guardrails.py` as a thin enforcement layer over `Policy`: one
> `check(action, target_node, url)` returning allow / block / escalate with a
> reason, plus a `redact` helper. It must be impossible to reach the surface
> without passing through it — wrap the surface rather than relying on callers.
>
> Implement `keystone/llm.py`: the `openai` SDK pointed at `LLM_BASE_URL`, temperature
> 0, structured JSON output, exponential backoff with jitter on 429 capped per
> `Settings`, and a `configured` guard so replay never needs a key.

**Checkpoint:** if the model emitted a click on "Post Transaction", trace
exactly what happens and where it stops.

---

## Phase 4 — replay (before discovery — this order matters)

Replay needs an artifact and discovery does not exist yet, so hand-write one.
That is deliberate: it forces the schema to be usable by a human, and gives the
discovery compiler a target shape to produce.

> Hand-write `artifacts/lookup_member_savings_balance.v1.json` conforming to
> `CapabilityArtifact`: navigate to the pinnacle lookup page, type a
> `member_id` param into the member field, click Search, and read the Regular
> Savings Balance into a typed output. Include a checkpoint on every step, a
> success condition, business outcomes for MEMBER_NOT_FOUND and
> PERMISSION_DENIED, and a recovery rule for the message-of-the-day dialog.
>
> Then implement `keystone/replay/engine.py`: validate params against their specs,
> apply any tenant override, and for each step resolve the locator, pass the
> guardrail, act, and assert the checkpoint. On checkpoint failure, check the
> declared business outcomes first, then the recovery rules (bounded by
> `max_attempts`), then escalation rules, and only then report a hard failure
> with `failed_step`, `expected`, `observed` and a screenshot. Extract outputs
> at the end and return a `ReplayResult`.
>
> No LLM anywhere in this file.

Verify each branch by hand:

```bash
python -m keystone replay lookup_member_savings_balance --member_id 12345   # success
python -m keystone replay lookup_member_savings_balance --member_id 99999   # business
python -m keystone replay lookup_member_savings_balance --member_id 44120   # business
curl -XPOST localhost:8800/_faults/interstitial/arm
python -m keystone replay lookup_member_savings_balance --member_id 22881   # recovered
curl -XPOST localhost:8800/_faults/server_error/arm
python -m keystone replay lookup_member_savings_balance --member_id 12345   # failure
```

**Checkpoint:** why is `ReplayResult.ok` true for a business outcome? What
distinguishes a recoverable condition from a business outcome *mechanically*,
not conceptually?

---

## Phase 5 — discovery

> Implement `keystone/discovery/prompts.py`, `agent.py` and `compiler.py`.
>
> The loop: observe, render the observation compactly, ask the model for
> exactly one typed action, validate the ref against the current observation,
> pass the guardrail, act, record. Stop on model-declared done, max steps, run
> timeout, an unchanged-observation counter, or repeated failures. On done,
> evaluate the success condition yourself against the live surface — never
> trust the claim.
>
> Treat page content as untrusted data in the prompt and constrain the model's
> output to a JSON action schema so it has no channel for free-form
> instructions.
>
> The compiler turns the recorded trace into a `CapabilityArtifact`: refs
> become `AXLocator`s with rationales and fallbacks; literals that came from
> the goal become typed params; concrete routes become `route_template`s; the
> node the model identified as the answer becomes an `Extraction`; each step
> gets a checkpoint synthesised from the observation after it; sensitive-field
> values become params with no stored literal.
>
> After compiling, replay the artifact once with a *different* parameter value.
> Only save it as `draft` if that passes.

Then the real run the brief requires:

```bash
python -m keystone discover "look up member 12345 and read their regular savings balance" \
  --entry http://127.0.0.1:8800/t/pinnacle/lookup --tenant pinnacle
```

**Checkpoint:** name four ways the model could hallucinate here and the
specific mechanism that catches each.

---

## Phase 6 — escalation and handoff

> Implement `keystone/escalation/controller.py` and `console.py`. Detect stuck
> (no-progress, repeated failure, unrecognised state) and risky-action
> interception. Raise an intervention request carrying the capability or goal,
> the current step, the surface state, a screenshot, and why it stopped.
>
> Expose the **same live browser session** for manual control — run headed,
> hand the operator the session, and have a minimal WebSocket console show the
> request and offer resume. On resume, re-observe and re-evaluate which step's
> precondition currently holds; do not blindly continue at step N+1.
>
> Mock the operator UI deliberately and note in REPORT.md what is mocked.

**Checkpoint:** why re-evaluate instead of resuming at the next step? What
breaks if you don't?

---

## Phase 7 — the CLI and the capability catalog

> Implement `keystone/cli.py` with `discover`, `replay`, `approve`, and `catalog`.
> `catalog` lists saved artifacts as OpenAI-style tool schemas via
> `to_tool_schema()` — this is the agent-facing capability interface, and it is
> the one stretch goal worth having. `approve` moves an artifact from draft to
> approved and records who and when. Unattended replay refuses a draft.

---

## Phase 8 — tests

> Fill out `tests/` per the priorities in CLAUDE.md. Replay tests must run with
> no API key.

---

## Phase 9 — evidence and the write-up

Produce the real evidence directory: one genuine LLM discovery run, one
successful replay, one replay hitting an injected fault. Then write `README.md`
(setup, config, exact demo commands) and `REPORT.md` under the seven required
headings.

Write REPORT.md **yourself**, not with Claude Code. It is the artifact they
read most closely for your reasoning, and it should sound like you.

---

## Cross-tenant stretch (only if phases 1–9 are done)

Record against pinnacle, then add a `TenantOverride` for harbor and replay the
same artifact against it. The field reorder should need no override at all —
that is the whole argument for accessible-name targeting. The `Member #:` →
`Account Number:` rename needs exactly one. That contrast is the demo.
