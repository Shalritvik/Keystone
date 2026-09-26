"""Tests for grip/escalation/controller.py and its wiring into ReplayEngine.

The pure test below needs no browser -- EscalationController's blocking
behavior is asyncio machinery, testable with the same FakeSurface pattern
used in test_guardrails.py. The integration tests need the real mock app
because they exercise a genuine risky-action guardrail interception end to
end, mirroring scripts/run_escalation_demo.py as proper assertions.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from grip.escalation.controller import EscalationController
from grip.evidence import EvidenceWriter
from grip.guardrails import GuardedSurface, Guardrails
from grip.replay.engine import ReplayEngine
from grip.schemas import AXLocator, CapabilityArtifact
from grip.surface.base import ActionOutcome, ActionRequest, AXNode, Observation, Surface

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT_PATH = REPO_ROOT / "artifacts" / "open_subaccount.v1.json"
POST_BUTTON = AXLocator(role="button", name="Post Transaction", name_match="normalized", frame_path=[], ordinal=0)


class FakeSurface(Surface):
    kind = "fake"

    async def observe(self) -> Observation:
        return Observation(url="http://x/review", title="t", nodes=[AXNode(ref="n0", role="status", name="ok")])

    async def act(self, request: ActionRequest) -> ActionOutcome:
        return ActionOutcome(ok=True)

    async def resolve(self, locator):
        return "n0", "primary"

    async def screenshot(self, path: str) -> bool:
        return False  # exercise the "no screenshot available" path too

    async def current_url(self) -> str:
        return "http://x/review"

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_raise_intervention_blocks_until_resumed(tmp_path):
    controller = EscalationController(FakeSurface(), EvidenceWriter(tmp_path))
    assert controller.control == "automation"

    raised = asyncio.Event()

    async def escalate():
        raised.set()
        return await controller.raise_intervention(
            run_id="r1", capability_id="cap", goal=None, step_index=3,
            reason="risky", screenshot_dir=tmp_path,
        )

    task = asyncio.create_task(escalate())
    await raised.wait()
    await asyncio.sleep(0.05)  # let raise_intervention actually reach the await

    assert not task.done()
    assert controller.control == "human"
    assert controller.pending is not None
    assert controller.pending.reason == "risky"
    assert controller.pending.screenshot_path is None  # FakeSurface.screenshot() returns False

    resolved = controller.resume(note="handled it")
    request = await task

    assert task.done()
    assert controller.control == "automation"
    assert controller.pending is None
    assert request.status == "resumed"
    assert request.note == "handled it"
    assert resolved.request_id == request.request_id


def test_resume_without_pending_raises(tmp_path):
    controller = EscalationController(FakeSurface(), EvidenceWriter(tmp_path))
    with pytest.raises(RuntimeError):
        controller.resume()


@pytest.fixture
def artifact() -> CapabilityArtifact:
    return CapabilityArtifact.model_validate(json.loads(ARTIFACT_PATH.read_text()))


@pytest.mark.asyncio
async def test_unattended_risky_step_escalates_with_no_controller(guarded_surface, tmp_path, artifact):
    engine = ReplayEngine(guarded_surface, tmp_path)
    result = await engine.run(artifact, {"member_id": "12345"})
    assert result.status == "escalated"
    assert result.escalation_id == "GUARDRAIL_ESCALATION"


@pytest.mark.asyncio
async def test_attended_handoff_resumes_and_completes(guarded_surface, tmp_path, artifact):
    """Mirrors scripts/run_escalation_demo.py's Scenario B as an assertion:

    pause, a "human" acts directly on the raw surface (bypassing the
    guardrail), resume, and the run must re-check the step's own checkpoint
    rather than blindly retrying the original (now-impossible) click.
    """
    raw = guarded_surface._surface  # the unwrapped PlaywrightSurface, for the "human" to act on directly
    writer = EvidenceWriter(tmp_path / "controller-log")
    controller = EscalationController(guarded_surface, writer)
    engine = ReplayEngine(guarded_surface, tmp_path, escalation_controller=controller)

    run_task = asyncio.create_task(engine.run(artifact, {"member_id": "12345"}))

    while controller.pending is None and not run_task.done():
        await asyncio.sleep(0.05)

    assert controller.pending is not None
    assert controller.pending.step_index == 3
    assert controller.control == "human"

    ref, how = await raw.resolve(POST_BUTTON)
    assert ref is not None, how
    outcome = await raw.act(ActionRequest(action="click", ref=ref))
    assert outcome.ok

    controller.resume(note="operator posted it")
    result = await run_task
    writer.close()

    assert result.status == "success"
    posted_step = next(s for s in result.steps if s.index == 3)
    assert posted_step.status == "recovered"
    assert "human intervention" in posted_step.detail
