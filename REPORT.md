# Keystone: Computer-Use Automation System

Keystone turns a goal demonstrated through a legacy application's UI into a reusable capability. An LLM discovers the flow; subsequent invocations execute a reviewed artifact without model decisions. The implemented target is a local credit-union servicing application with synthetic data, framesets, table-based labels, tenant variations, and injectable runtime faults.

## Architecture

I separated discovery from execution: discovery explores, replay executes a declared contract and stops the moment the application departs from it.

`discovery/agent.py` runs an observe-decide-act loop through an OpenAI-compatible client (`openai/gpt-oss-20b` on NVIDIA NIM), one structured action per response. It validates references against the current observation, enforces policy, records successful actions, and stops on completion, step/time budgets, repeated failures, or no progress, with bounded backoff on provider rate limits. Replay makes no model calls, so provider latency only affects discovery.

The compiler converts the trace into a `CapabilityArtifact`. Before saving, discovery evaluates the synthesized success condition live, then replays the draft in a fresh browser context with alternate parameters (discovers member 12345, verifies 22881) — rejecting false completions and hard-coded values, though not proving the condition fully captures the goal. Human review remains necessary.

Both paths use `GuardedSurface`, wrapping a six-operation `Surface` interface: observe, act, resolve, screenshot, current URL, close. The Playwright adapter owns browser details; the replay engine owns flow execution. `catalog --state approved` exposes callable tool schemas with no browser knowledge required.

## Artifact schema

The Pydantic schema in `schemas.py` treats a capability as an interface with explicit behavior:

| Contract area | Representation and purpose |
|---|---|
| Identity and binding | Schema version, capability ID/version, entry point, vendor product/version, and recorded tenant. |
| Inputs and outputs | Parameter specifications, validation patterns, sensitivity flags, output types, extraction locators, and required-value flags. |
| Execution | Ordered actions, literal/parameter/previously-extracted value sources, route templates, checkpoints, and locator rationales. |
| Exceptional states | Declared business outcomes, bounded recovery rules, and escalation conditions. |
| Review | Draft/approved/deprecated state, provenance, policy version, and executable-content hash. |

Live references such as `n3` are transient, never used as artifact targets. Durable `AXLocator`s store role, name-matching mode, frame path, optional ancestor name, ordinal, and ordered fallbacks with stated weaknesses. The primary resolver uses role, name, frame, ancestor name, and ordinal — the recorded ancestor-role chain isn't enforced.

`member_id` becomes a parameter; the balance comes from a named value cell. Discovery emits string-typed inputs/outputs and derives checkpoints from observed actions — richer types and outcome rules need manual authoring, since a happy-path recording gives no evidence for permission-denied or not-found handling.

Discovered artifacts start as drafts; unattended replay requires approval. With parameters supplied, `approve` repeats replay and checks usable-result rate and primary-locator resolution rate, requiring an override on failure. Approval without parameters skips that check — an operator decision, not an automatic certification.

## Determinism & error handling

Replay binds parameters, resolves stored locators, acts through policy, checks postconditions, then independently re-resolves and reads outputs at the end. Determinism means fixed rules with no LLM decisions — live data can still change between invocations.

On a failed action or checkpoint, replay checks business outcomes, attempts bounded recovery, checks escalation rules, then fails hard — a risky-action escalation always takes priority. Interstitials are cleared proactively after successful actions, and recovery counts persist across the run to prevent endless retries.

The result contract distinguishes success (outputs), business (a stable outcome code), failure (debugging context), and escalated (human action required). The lookup artifact returns `MEMBER_NOT_FOUND` for 99999 and `PERMISSION_DENIED` for 44120; an injected server error fails the balance-panel checkpoint — different answers, not interchangeable exceptions.

Playwright provides action timeouts and actionability waits, and steps can declare a settling delay, but checkpoints are evaluated once — delayed state can cause a premature failure. Session expiry and validation errors still need explicit rules; fault injection alone doesn't implement them.

Evidence is append-only JSONL, flushed and fsynced per event, plus an atomic result; step-start/finish records expose interrupted actions, though only per-step failures capture a screenshot. The [evidence index](evidence/README.md) links discovery, verification, attended replay, a business outcome, and an injected failure — some committed runs carry an earlier content hash and should be refreshed before submission.

## Heterogeneity & multi-tenant

The browser adapter computes an accessibility-like representation from DOM elements across frames, using semantic naming where available and adjacent table-cell labels where the app provides none — avoiding clean markup or test IDs as a prerequisite. It's DOM-backed perception, not general screenshot-based computer use, and its label/value heuristic can misread headerless data grids, a documented limitation.

The `Surface` boundary would let a future Windows UI Automation or macOS accessibility adapter return the same observations and actions; custom-drawn controls would need additional visual targeting. Desktop execution is designed, not implemented — orchestration still constructs the web adapter directly.

Reuse is a vendor-product capability plus small tenant overrides: entry point, step locators, defaults, disabled steps. Harbor reorders fields and renames the member field and search button; the artifact patches the entry and two captions (reordering needs none), updating any matching checkpoint alongside its override — covered by the cross-tenant replay test.

Product/version metadata is descriptive, not enforced. Production would need compatibility checks, tenant-specific policy, and review of every affected checkpoint and output before accepting overrides. Resolution telemetry gives a drift signal, but the reliability check samples action targets only, not every checkpoint or extraction locator.

## Escalation & handoff

`EscalationController` raises an intervention carrying the run, capability/goal, step, reason, screenshot, and surface summary. Ownership passes to the human; the calling coroutine waits on an explicit resume signal while retaining the same browser session — in headed operation, a person can operate that window directly. A minimal localhost WebSocket console exposes status and resume with an operator note.

On resume, replay checks the paused step's own checkpoint: if the person completed it, execution continues; otherwise the step retries — avoiding both silently skipping unfinished work and repeating a completed action. Repeated unresolved handoffs are capped, and discovery can request the same help for no-progress or repeated-failure states when a controller is supplied; fixed step/time limits still remain terminal.

The escalation demo simulates an operator clicking the mock app's real risky control through the same session, then resuming — the transport and pause/resume mechanism are real, the human is simulated. Ordinary replay doesn't wire in this controller, and `--attended` permits risky actions under blanket supervision rather than per-action confirmation. Operator notes record interventions, not a full capture of manual actions. Authentication, disconnect handling, and enforced session ownership remain future work.

## Safety

`policy.yaml` restricts origins, routes, and action types with explicit deny routes; control-name patterns distinguish safe, risky, and forbidden actions. Unattended risky controls escalate, forbidden ones stay blocked even when attended — executable checks, independent of the prompt. Synthetic local data lets the demo exercise banking-like workflows with no real credentials or customer records.

Sensitive field patterns redact flagged values in evidence and turn sensitive inputs into parameters with no stored example — partial protection only: goals, model explanations, rendered targets, screenshots, and outputs can still carry sensitive data, and discovery observations reach the model provider too. The system doesn't yet guarantee against persisting or disclosing regulated data in production.

Other boundaries need hardening: action checks don't intercept every redirect, link, form submission, or frame/network call; caption-based risk classification can miss an unfamiliar control; refs carry no observation-generation token. CLI checks catch some edits, but hashes are unsigned, empty seals are accepted, and a direct engine caller bypasses approval entirely. These are demo guardrails, not authorization or isolation guarantees.

## Cuts

I prioritized a working browser vertical slice and explicit contracts over queues, clusters, a database, desktop automation, or a polished operator dashboard. Automatic discovery of exception rules was left out deliberately — those need observed examples and review — and there's no open-ended model fallback during replay.

Next, in rough order: centralize approval and policy enforcement at invocation; add strict type validation and stronger, goal-specific success conditions; implement bounded condition polling; require version-aware tenant compatibility; and, before real financial data, add comprehensive redaction, protected evidence storage, authenticated operator control, observation-bound targeting, and network/destination enforcement. Write operations need duplicate-action prevention and verified commit boundaries before automatic retries. Finally, refresh evidence against the final artifact hashes and add one desktop adapter to test the abstraction beyond the browser.
