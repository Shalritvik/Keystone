# Architecture

All layers below are built and verified live against the mock: discovery,
replay, guardrails, evidence, and escalation. The known gaps are tracked
explicitly in §4, not hidden by this status line.

## 1. The whole system

Everything follows from one split: the model runs **once**, during discovery.
The path that touches production has no model in it at all.

```
                            ┌──────────────────────────┐
                            │    CALLING AI AGENT      │
                            │   (decides WHAT to do)   │
                            └────┬────────────────┬────┘
                 goal, once      │                │   capability_id + params,
                                 │                │   every time after
                                 ▼                ▼
  ┌──────────────────────────────────┐  ┌──────────────────────────────────┐
  │  DISCOVERY              [BUILT]  │  │  REPLAY                 [BUILT]  │
  │  ······························  │  │  ······························  │
  │  LLM in the loop                 │  │  NO LLM anywhere                 │
  │  slow, rate-limited, one-off     │  │  fast, free, constant            │
  │  staging / synthetic data        │  │  production data                 │
  │  non-deterministic               │  │  deterministic                   │
  │                                  │  │                                  │
  │   ┌─────────┐    ┌────────────┐  │  │   ┌──────────────────────────┐   │
  │   │  agent  │───►│  compiler  │  │  │   │      replay engine       │   │
  │   │  loop   │    │ trace→art. │  │  │   │  resolve→act→checkpoint  │   │
  │   └────┬────┘    └─────┬──────┘  │  │   └────────────┬─────────────┘   │
  │        │ 1 call/step   │         │  │                │                 │
  │        ▼               │         │  │                │                 │
  │   ┌─────────┐          │         │  │                │                 │
  │   │ llm.py  │          │         │  │                │                 │
  │   │ NIM API │          │         │  │                │                 │
  │   └─────────┘          │         │  │                │                 │
  └────────────────────────┼─────────┘  └────────────────┼─────────────────┘
                           │ writes              reads   │
                           ▼                             │
                   ┌────────────────────┐                │
                   │   ARTIFACT STORE   │────────────────┘
                   │  draft → approved  │
                   │  content-hashed    │
                   └────────────────────┘

        Both paths issue every single action through the same two layers:

                ┌───────────────────────────────────────────┐
                │  GUARDRAILS   (policy.yaml)      [BUILT]  │ ◄─ a gate,
                │  origin · route · action type · risk      │    not a prompt
                └─────────────────────┬─────────────────────┘
                                      ▼
                ┌───────────────────────────────────────────┐
                │  SURFACE   (abstract, 6 methods) [BUILT]  │
                │  observe · act · resolve · screenshot     │
                └───────┬───────────────────────────┬───────┘
                        ▼                           ▼
             ┌─────────────────────┐   ┌─────────────────────────┐
             │  PlaywrightSurface  │   │  DesktopSurface         │
             │  injected accname   │   │  macOS AX API / Win UIA │
             │  BUILT + VERIFIED   │   │  DESIGNED ONLY          │
             └──────────┬──────────┘   └─────────────────────────┘
                        ▼
             ┌─────────────────────┐
             │  legacy bank app    │
             │  mock: frameset,    │
             │  no test IDs [BUILT]│
             └─────────────────────┘

  cross-cutting:  EVIDENCE (jsonl) [BUILT]   ESCALATION (human) [BUILT]
```

---

## 2. Perception — inside `_scan()`

The adapter does **not** read Chrome's computed accessibility tree. It runs
one injected script that recomputes the accname chain itself, because the
target app labels fields by the table cell physically to their left — a
visual convention no browser computes, because it is not in any spec.
Running CDP *and* a DOM fallback would mean two name computations that can
disagree about the same node. There is exactly one.

```
  _scan()                          two round-trips per FRAME, not per element
  ┌──────────────────────────────────────────────────────────────┐
  │  dispose the previous ref_map's handles                      │
  └────────────────────────────┬─────────────────────────────────┘
                               ▼
              for each live frame in page.frames:
  ┌──────────────────────────────────────────────────────────────┐
  │  (1) eval_on_selector_all(_SELECTOR, _DESCRIBE_ALL_JS)       │
  │         -> [{role, name, value, visible, disabled, skip,…}]  │
  │  (2) query_selector_all(_SELECTOR)                           │
  │         -> [ElementHandle]                                   │
  │                                                              │
  │  back-to-back with no await between, so index alignment      │
  │  holds. A length mismatch means the DOM mutated mid-scan     │
  │  -> skip the frame rather than misattribute a description.   │
  └────────────────────────────┬─────────────────────────────────┘
                               ▼
                       zip(descs, handles)
                               │
             skip? or not visible? ──yes──►  dropped
                               │ no          (handle NOT disposed — see §4)
                               ▼
                  ref = "n" + counter++
                  ordinal = how many identical
                    (role, name, frame, ancestors) already seen
                               │
                               ▼
                  AXNode    and    ref_map[ref] = handle
```

### The accessible-name chain — first hit wins

```
  computeName(el)
  ├─ aria-labelledby                    ARIA
  ├─ aria-label                         ARIA      dialog "System Message"
  ├─ label[for=id]                      HTML      (absent in this app)
  ├─ wrapping <label>                   HTML      (absent in this app)
  ├─ value attribute  (submit/button)             "Search" "Find" "Continue"
  ├─ alt
  ├─ title
  ├─ placeholder
  ├─ paired-cell name                   LEGACY    static label:value rows
  ├─ own text content  (never <select>)           "NO RECORD FOUND FOR …"
  └─ adjacent LEFT cell's text          LEGACY    "Member #:"  form fields
                                                  "Account Number:" (harbor)
```

The last two are the app-specific idiom. Everything above them is standard.

### What is deliberately not emitted

```
  a <td> that only wraps a control   -> skip; the control IS the node
  the LABEL half of a label:value    -> skip; its text is already carried
    pair                                as the VALUE half's name
  anything not visible               -> skip
```

That second rule is the S1 fix, and it is the difference between a correct
extraction and a silent wrong answer:

```
   BEFORE                                AFTER
   cell "Regular Savings Balance:"       (label half dropped)
   cell "Regular Savings Balance:"       cell "Regular Savings Balance:"
        ='4,182.55'                           ='4,182.55'       ordinal 0

   ordinal 0 -> the CAPTION              ordinal 0 -> the VALUE
   read() -> "Regular Savings            read() -> "4,182.55"
              Balance:"   ok=True
              ^ wrong answer, reported as success
```

Measured on the member detail page: 33 nodes → 19 nodes, observe 318ms →
146ms, resolve 229ms → 65ms.

---

## 3. Ref lifecycle — and the open hazard

A `ref` is valid for exactly one observation. An `AXLocator` is what gets
persisted. Conflating the two is how automation becomes brittle, so they are
separate types and a ref is never serialised.

Refs are assigned positionally, in DOM order. That makes them **stable across
scans of an unchanged page and silently wrong across a changed one**:

```
   observation 1                    observation 2  (error banner inserted)
   n5  combobox "Branch:"           n5  cell     "Branch:"
   n6  button   "Search"            n6  combobox "Branch:"
   n7  button   "Clear"      ✗      n7  button   "Search"   ← same ref,
                                    n8  button   "Clear"      new element

   caller reasoned over obs 1 and asks:  click n7   ("Clear")
   guardrail classifies  "Clear"  ->  SAFE, allow
   surface looks up n7 in the CURRENT map  ->  clicks "Search"
   result:  ok=True, no error, wrong control acted on

   In this app that is a spurious search. In the real target, a ref shifting
   from "Back" to "Post Transaction" is the irreversible action the guardrail
   exists to prevent — and the guardrail does not catch it, because it judges
   the node the CALLER passed while the surface acts on a different element.
```

The fix, not yet applied:

```
   scan()   ->  self._scan_id += 1
   Observation.observation_id  = self._scan_id
   ActionRequest.observation_id = <the id the caller reasoned over>
   act():  id mismatch  ->  ActionOutcome(ok=False, error_kind="not_found")

   turns a silent mis-click into a loud, recoverable failure
```

---

## 4. Known gaps in the surface

```
  N1  stale refs act on the wrong control silently     OPEN — see §3
  N2  handles for skipped nodes are never disposed     OPEN
        35 matched -> 19 kept -> 16 leaked per scan
  N3  statusFrame yields zero nodes (text_digest only) OPEN, minor
  N4  Observation.step_index never populated           OPEN, trivial
  --  accounts table mis-naming                        DOCUMENTED, won't fix
        4 plain <td> per row with no <th> is
        structurally identical to 2 label:value pairs
```

---

## 5. Discovery control flow

```
  START  goal + entry_url + tenant
    │
    ▼
  open browser context ──► navigate(entry) ──[policy]──fail──► ABORT
    │
    ▼
 ┌──────────────────────────── LOOP ────────────────────────────┐
 │                                                               │
 │   observe()  ──►  nodes + observation_id                      │
 │      │                                                        │
 │      ▼                                                        │
 │   prompt = system + goal + short history + observation        │
 │      │            (page content marked UNTRUSTED DATA)        │
 │      ▼                                                        │
 │   LLM  ──►  exactly one typed action                          │
 │             {thought, action, ref, value, done}               │
 │      │                                                        │
 │      ▼                                                        │
 │   ┌───────────────── VALIDATE ──────────────────┐             │
 │   │  ref present in THIS observation?           │──no──┐      │
 │   │  observation_id still current?              │      │      │
 │   │  action type in allowlist?                  │      │      │
 │   │  risk(action, target caption)?              │      │      │
 │   └───────────────────┬─────────────────────────┘      │      │
 │              safe     │     risky / forbidden          │      │
 │                       │              ▼                 ▼      │
 │                       │          ESCALATE        error fed    │
 │                       ▼                          back as obs  │
 │                 surface.act()                          │      │
 │                       │                                │      │
 │                       ▼                                │      │
 │        record step + the node's durable locator ◄──────┘      │
 │                       │                                       │
 │                       ▼                                       │
 │     done? · max_steps? · timeout? · no-progress ×3? ──no───────┘
 │                       │ yes
 └───────────────────────┼───────────────────────────────────────┘
                         ▼
        VERIFY the success condition ourselves, on the live surface
        (the model's claim is never trusted)
                         │
                  holds  │  does not hold ──► FAIL / escalate
                         ▼
        COMPILE: refs→AXLocators(+rationale,+fallbacks)
                 literals→typed params
                 /member/12345 → /member/:member_id
                 answer node → Extraction
                 post-states → checkpoints
                 sensitive fields → params with no stored literal
                         │
                         ▼
        VALIDATE: replay once with a DIFFERENT param value
                         │
                pass ────┴──── fail ──► discard + report why
                  ▼
           save as DRAFT ──► human approval ──► APPROVED
```

---

## 6. Replay control flow — and the error taxonomy

The classification order is the design, not an implementation detail.

```
  START  artifact + params + tenant
    │
    ▼
  validate params ────invalid────► FAILURE
    ▼
  content_hash == approved? ────mismatch────► FAILURE (tampered)
    ▼
  artifact.for_tenant(tenant)          ← per-tenant overrides applied
    ▼
  NEW browser context (one per run, never reused)   ← tenant isolation
    │
 ┌──────────────────────── for each step ───────────────────────┐
 │   resolve(locator)  ──► (ref, how) + observation_id           │
 │     ├─ primary   role + name + frame + ancestor               │
 │     ├─ fallback₁ …        (each records why it is weaker)     │
 │     └─ nothing matched ─────────────────────────┐             │
 │              ▼                                  │             │
 │   guardrail.check() ───block───► FAILURE        │             │
 │              ▼                                  │             │
 │   surface.act()  (carrying observation_id)      │             │
 │              ▼                                  ▼             │
 │   assert checkpoint ───holds───► next step                    │
 │              │ fails                                          │
 │              ▼                                                │
 │          ┌───────────────── CLASSIFY ─────────────────┐       │
 │          │  1  business_outcome.detect matches?       │       │
 │          │        ──► BUSINESS(code)   NOT an error   │       │
 │          │            MEMBER_NOT_FOUND / NOT_ENTITLED │       │
 │          │                                            │       │
 │          │  2  recovery.detect matches?               │       │
 │          │        and attempts < max_attempts         │       │
 │          │        ──► apply action, retry step ───────┼───────┘
 │          │            e.g. dismiss "System Message"   │
 │          │                                            │
 │          │  3  escalation.detect matches?             │
 │          │        ──► ESCALATED                       │
 │          │            e.g. session expired            │
 │          │                                            │
 │          │  4  none of the above                      │
 │          │        ──► FAILURE(unrecognised_state)     │
 │          │            + step + expected/observed      │
 │          │            + screenshot + AX snapshot      │
 │          └────────────────────────────────────────────┘
 └───────────────────────────────────────────────────────────────┘
    │ all steps completed
    ▼
  assert overall success condition ──fails──► same CLASSIFY tree
    ▼
  extract outputs via Extraction locators (read FROM the surface)
    ▼
  SUCCESS(outputs)


  ┌──────────────────────────────────────────────────────────────┐
  │  RESULT CONTRACT                                             │
  │    success   completed, condition held, outputs present      │
  │    business  a declared, expected non-success answer         │
  │    failure   everything else, with debuggable detail         │
  │    escalated stopped and handed to a human                   │
  │                                                              │
  │    result.ok == (success OR business)                        │
  │      "did this work?" and "did this succeed?" are            │
  │      different questions, and callers ask both.              │
  └──────────────────────────────────────────────────────────────┘
```

Business outcomes are checked **first** because a "no record found" screen is
indistinguishable from a failed checkpoint unless you look for it
deliberately, and conflating them is the most common design mistake in this
problem. Recovery second, so a dismissable interstitial is not a failure.
Escalation third. Hard failure last, and only for states nobody declared —
a system that improvises against unknown states inside a bank is worse than
one that stops cleanly.

All four detection surfaces are verified present in the mock:

```
  dialog  "System Message"                       -> recovery trigger
  status  "NO RECORD FOUND FOR MEMBER 99999"     -> MEMBER_NOT_FOUND
  alert   "OPERATOR NOT ENTITLED TO VIEW …"      -> NOT_ENTITLED
  alert   "Member number must be numeric."       -> validation outcome
```

---

## 7. Escalation and control transfer

```
   automation (discovery or replay)
        │
        │   stuck  |  risky action  |  unrecognised state
        ▼
  ┌────────────────────────────┐
  │  PAUSE automation          │   the browser context STAYS ALIVE —
  │  (do not close the context)│   this is the entire point
  └────────────┬───────────────┘
               ▼
  ┌──────────────────────────────────────────────┐
  │  InterventionRequest                         │
  │    run_id, capability or goal                │
  │    current step index + what it was trying   │
  │    surface state + screenshot                │
  │    why it stopped                            │
  └────────────┬─────────────────────────────────┘
               ▼
      operator console  (WebSocket)
               │
               ▼
  ┌────────────────────────────┐
  │  HUMAN drives the SAME     │   same session, same cookies,
  │  live session              │   same page — never a fresh one
  └────────────┬───────────────┘
               │  operator signals "resume"
               ▼
  ┌──────────────────────────────────────────────┐
  │  RE-OBSERVE and RE-EVALUATE                  │
  │  which step's precondition holds NOW?        │
  │                                              │
  │  Never blindly continue at step N+1: the     │
  │  human may have moved the app forward,       │
  │  backward, or somewhere else entirely.       │
  │  (Also invalidates every ref — see §3.)      │
  └────────────┬─────────────────────────────────┘
               ▼
     resume  |  complete  |  abort
               ▼
     all recorded as evidence, including what the human did
```

---

## 8. Trust boundaries

```
  ┌─────────────────────── UNTRUSTED ────────────────────────┐
  │  page content · accessible names · control values        │
  │  a compromised page can contain "ignore all previous…"   │
  └──────────────────────────┬───────────────────────────────┘
                             │  delivered as delimited DATA,
                             │  never as instructions
                             ▼
  ┌──────────────────────────────────────────────────────────┐
  │  MODEL                                                   │
  │  output constrained to {action, ref, value}              │
  │  no free-form channel ⇒ no way to emit instructions      │
  └──────────────────────────┬───────────────────────────────┘
                             ▼
  ┌──────────────────────────────────────────────────────────┐
  │  GUARDRAIL — evaluated independently of the model        │
  │  origin allowlist · route · action type · caption risk   │
  │  a fully injected model still cannot leave the allowlist │
  │                                                          │
  │  ⚠ GAP: it judges the node the caller passed. A stale    │
  │     ref makes the surface act on a different element,    │
  │     bypassing this check entirely. See §3.               │
  └──────────────────────────┬───────────────────────────────┘
                             ▼
  ┌──────────────────────────────────────────────────────────┐
  │  SURFACE — one browser context per run, never reused     │
  └──────────────────────────────────────────────────────────┘


  Where data goes:

    synthetic / staging data ──► discovery ──► LLM provider
    PRODUCTION data          ──► replay    ──► caller only
                                             (replay calls no model, so
                                              member data never reaches
                                              an LLM)

    artifacts   structure only — locators and param NAMES, never values
    evidence    redacted in the record constructor, at write time
    outputs     returned to the caller, not persisted by this layer
```

---

## 9. Anatomy of a capability artifact

```
  CapabilityArtifact
  │
  ├── IDENTITY
  │     capability_id · version · content_hash · approval(draft→approved)
  │     provenance (model, run id, when, policy version)
  │
  ├── CONTRACT              ← what a calling agent reads
  │     params[]    typed inputs   (name, type, pattern, sensitive?)
  │     outputs[]   typed outputs  (name, type, Extraction locator)
  │     success     Condition      how we know it worked
  │
  ├── EXECUTION             ← what the replay engine reads
  │     surface     entry · product · product_version · tenant
  │     steps[]     index · action · AXLocator · ValueSource
  │                 · checkpoint · settle_ms · risk · route_template
  │
  └── EXCEPTIONS            ← the part that makes it production-usable
        business_outcomes[]   legitimate non-success answers + detection
        recoveries[]          bounded detect-then-do rules, attempt-capped
        escalations[]         states that must go to a human
        tenant_overrides[]    narrow per-institution locator diffs


  AXLocator — how a control is found months later
  ┌────────────────────────────────────────────────────────┐
  │  role           "cell"                                 │
  │  name           "Regular Savings Balance:"             │
  │  name_match     normalized                             │
  │  frame_path     ["contentFrame"]  ← frameset disambig. │
  │  ancestor_roles [...]             ← region disambig.   │
  │  ordinal        0                 ← >0 is a smell      │
  │  fallbacks[]    css / xpath, each with weaker_because  │
  │  rationale      "why this should still work"  REQUIRED │
  └────────────────────────────────────────────────────────┘
```

---

## 10. One request, end to end

Timings are measured against the mock, not estimates.

```
  agent: lookup_member_savings_balance(member_id="22881")
     │
     ├─ params validated ....................... matches ^\d{1,10}$
     ├─ hash verified .......................... == approved hash
     ├─ context created ........................ fresh, isolated
     │
     ├─ step 0  navigate  /t/pinnacle/lookup
     │            checkpoint url_matches /lookup ............... ok
     ├─ step 1  type      textbox "Member #:" [contentFrame] ← ${member_id}
     │            named via the adjacent-left-cell rule
     │            checkpoint ax_value_matches 22881 ........... ok
     ├─ step 2  click     button "Search" [contentFrame]
     │            nav frame also has link "Search" — frame_path
     │            is what makes this unambiguous
     │            checkpoint url_matches /member/ ............. ok
     ├─ step 3  read      cell "Regular Savings Balance:" ordinal 0
     │            -> savings_balance                    (post-S1 fix)
     │
     ├─ success condition: ax_present("Regular Savings Balance:") ... holds
     │
     └─► ReplayResult(
           status  = "success",
           outputs = {"savings_balance": "17,640.12"},
           steps   = [4 traces],
           evidence_dir = "evidence/rpl_8f21c4/"
         )

  perception cost per step:  ~65ms resolve + ~146ms checkpoint observe
  model calls: 0 · tokens: 0 · fully auditable
```
