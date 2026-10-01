# evidence/

One directory per run, named by run id.

    <run-id>/
      run.jsonl        append-only structured record of every step
      result.json      the final typed result (atomic write; absent = crashed)
      screenshots/     captured on failure only

Local runs are gitignored. The demo runs referenced from README.md are
committed explicitly.

## The six committed runs

1. **`discover-lookup_member_savings_balance_auto-f513e1c0/`** -- the
   required genuine LLM-driven discovery run (`openai/gpt-oss-20b` via
   NVIDIA NIM). Goal: "look up member 12345 and read their regular savings
   balance." Observe/decide/act against the live mock app, ending in a
   model-declared "done" that was then independently re-verified against
   the live surface, never trusted as a claim (design rule 6). `result.json`
   records the outcome; `run.jsonl` has every model decision (including its
   stated reasoning) alongside what was actually validated and executed.

2. **`verify-lookup_member_savings_balance_auto-74f89db4/`** -- the
   discovery run's own mandatory self-check: the compiled draft artifact,
   replayed once against a *different* real member (22881) on a fresh
   browser context, with the LLM nowhere in the loop. Only because this
   passed did discovery save the artifact at all
   (`artifacts/lookup_member_savings_balance_auto.v1.json`).

3. **`replay-lookup_member_savings_balance_auto-bf55ad96/`** -- that
   freshly-discovered artifact is still a *draft*, so an unattended replay
   of it is refused (see README.md's demo commands). This is the same
   capability replayed `--attended` instead -- the artifact is real and
   works, but hasn't yet passed a human's approval gate for unattended use.

4. **`replay-lookup_member_savings_balance-001abdaf/`** -- the hand-authored,
   *approved* artifact, replayed unattended with a member number that
   doesn't exist. `status: "business"`, `outcome_code: "MEMBER_NOT_FOUND"`.
   The host answered correctly; this is not an error. This is the one
   distinction the whole project is built around.

5. **`replay-lookup_member_savings_balance-0d459838/`** -- the same
   approved artifact, replayed with `server_error` armed on the mock app's
   fault switchboard. `status: "failure"`, `failure_kind:
   "checkpoint_failed"`, `failed_step: 1`, with `screenshots/` holding the
   actual page the run was looking at when it gave up. This is the required
   "replay that hits an injected fault."

6. **`discover-open_subaccount_auto-c3e6eb08/`** -- a second genuine
   LLM-driven discovery run, this time against the harder multi-step,
   multi-page sub-account flow (member # -> select type -> review -> post),
   with the goal explicitly asking it to complete the irreversible step. The
   model correctly selected "S07 - VACATION CLUB" from the live option list
   (see `AXNode.options`, added after this exact run first failed on a
   version of the fix that didn't exist yet -- the model had guessed the
   bare label "VACATION CLUB", which matched no option), reached the review
   screen, and clicked "Post Transaction" on its own reasoning. The guardrail
   correctly refused it three times (`unattended_risky_disposition: escalate`
   with no escalation controller wired into this CLI invocation, so refusal
   -- not a pause-and-resume -- is what unattended means here), and the
   bounded retry then stopped the run at `repeated_failures` rather than
   looping or guessing. `ok: false`, no artifact saved -- correctly, since
   the flow never completed. The pause/resume mechanics themselves (bounded
   retries, a controller's `raise_intervention`/`resume`) are exercised in
   `test_stuck_no_progress_still_escalates_and_gets_a_real_retry` in
   `tests/test_discovery_agent.py`, but only against a fake surface and a
   fake controller -- not this live browser session. Wiring a real,
   connected controller into this exact CLI path, so a human could actually
   resume past the refusal instead of it hard-stopping, is a known gap:
   `python -m keystone discover` has no `--escalate` flag the way `replay`
   does, and `discover()` builds its own browser session internally with no
   way for a caller to hand it a pre-wired controller for that same session.

Why two different artifacts across the first five: the auto-discovered one
(#1-3) only ever observed the happy path, so it has no declared business
outcomes yet -- correctly so, since rule 7 requires those to be
human-reviewed and added, never inferred. Runs #4-5 use the hand-authored
artifact specifically because it *does* declare them, to demonstrate the
full three-way contract cleanly rather than mixing that gap into the
failure demo. See REPORT.md's Cuts section.
