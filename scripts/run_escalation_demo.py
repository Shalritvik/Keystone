"""Demonstrates the full escalation/handoff mechanism against a real risky

step (mockapp's "Post Transaction" button, classified risky by policy.yaml).

Scenario A -- unattended, no controller: the run stops at the risky step
and reports status="escalated". No controller means nobody to hand off to.

Scenario B -- attended, with a controller: the SAME artifact, run in a
background task, pauses at the same point. A "human" then acts directly on
the raw (unwrapped) surface -- bypassing this project's own guardrail
entirely, exactly as OS-level mouse input to a visible, headed browser
window would -- clicks Post Transaction, and tells the console to resume.
The run re-checks the step's own checkpoint (not blindly step N+1),
recognises it's already satisfied, and finishes with status="success".

Usage:
    python scripts/run_escalation_demo.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from grip.config import Policy, Settings  # noqa: E402
from grip.escalation.controller import EscalationController  # noqa: E402
from grip.evidence import EvidenceWriter  # noqa: E402
from grip.guardrails import GuardedSurface, Guardrails  # noqa: E402
from grip.replay.engine import ReplayEngine  # noqa: E402
from grip.schemas import AXLocator, CapabilityArtifact  # noqa: E402
from grip.surface.base import ActionRequest  # noqa: E402
from grip.surface.web import PlaywrightSurface  # noqa: E402

ARTIFACT_PATH = Path(__file__).resolve().parent.parent / "artifacts" / "open_subaccount.v1.json"
POST_BUTTON = AXLocator(role="button", name="Post Transaction", name_match="normalized", frame_path=[], ordinal=0)


def load_artifact() -> CapabilityArtifact:
    return CapabilityArtifact.model_validate(json.loads(ARTIFACT_PATH.read_text()))


async def scenario_a_unattended(settings: Settings, policy: Policy) -> None:
    print("\n=== Scenario A: unattended, no controller ===")
    raw = await PlaywrightSurface.create(headless=settings.headless)
    guarded = GuardedSurface(raw, Guardrails(policy, attended=False))
    try:
        engine = ReplayEngine(guarded, settings.evidence_dir)
        result = await engine.run(load_artifact(), {"member_id": "12345"})
    finally:
        await guarded.close()

    print(f"status={result.status} escalation_id={result.escalation_id} message={result.message!r}")
    assert result.status == "escalated", "expected an unattended risky step to escalate"


async def scenario_b_attended_handoff(settings: Settings, policy: Policy) -> None:
    print("\n=== Scenario B: attended, real handoff ===")
    raw = await PlaywrightSurface.create(headless=settings.headless)
    guarded = GuardedSurface(raw, Guardrails(policy, attended=False))
    writer = EvidenceWriter(settings.evidence_dir / "escalation-demo-controller-log")
    controller = EscalationController(guarded, writer)

    try:
        engine = ReplayEngine(guarded, settings.evidence_dir, escalation_controller=controller)
        run_task = asyncio.create_task(engine.run(load_artifact(), {"member_id": "12345"}))

        while controller.pending is None and not run_task.done():
            await asyncio.sleep(0.05)

        request = controller.pending
        assert request is not None, "expected the risky step to raise an intervention"
        print(f"ESCALATION RAISED: step={request.step_index} reason={request.reason!r}")
        print(f"  screenshot: {request.screenshot_path}")
        print(f"  surface summary (first 200 chars): {request.surface_summary[:200]!r}")
        print(f"  control is now: {controller.control!r}")

        # The "human" acts on the RAW surface directly -- not through
        # `guarded` -- because a real operator clicking the visible,
        # headed browser window bypasses this project's Python guardrail
        # entirely. That is the point of the handoff: the human has their
        # own judgement and authority, not our automation's.
        ref, how = await raw.resolve(POST_BUTTON)
        assert ref is not None, f"human couldn't find the Post Transaction button ({how})"
        outcome = await raw.act(ActionRequest(action="click", ref=ref))
        print(f"  human clicked Post Transaction directly: ok={outcome.ok}")

        resolved = controller.resume(note="operator reviewed and manually posted the transaction")
        print(f"RESUMED: status={resolved.status} note={resolved.note!r}")

        result = await run_task
    finally:
        writer.close()
        await guarded.close()

    print(f"FINAL: status={result.status}")
    for trace in result.steps:
        print(f"  step {trace.index}: {trace.action} -> {trace.status} ({trace.detail})")
    assert result.status == "success", "expected the run to resume and complete after the handoff"


async def main() -> None:
    settings = Settings.from_env()
    policy = Policy.load(settings.policy_path)
    await scenario_a_unattended(settings, policy)
    await scenario_b_attended_handoff(settings, policy)
    print("\nBoth scenarios behaved as expected.")


if __name__ == "__main__":
    asyncio.run(main())
