"""Tests for keystone/discovery/agent.py's stuck-detection/escalation logic in

_run_loop. These are pure logic tests -- no browser, no LLM -- because the
scenarios exercised here (a hard bound already exceeded before the first
iteration) never reach either one.
"""

from __future__ import annotations

import pytest

from keystone.config import Policy, Settings
from keystone.discovery.agent import MAX_DISCOVERY_ESCALATIONS, _run_loop
from keystone.evidence import EvidenceWriter
from keystone.guardrails import Guardrails
from keystone.surface.base import ActionOutcome, Observation


class _FakeEscalationController:
    def __init__(self) -> None:
        self.calls = 0

    async def raise_intervention(self, **kwargs) -> None:
        self.calls += 1


class _NeverCalledLLM:
    async def complete_json(self, **kwargs):
        raise AssertionError("the model must not be called once a hard bound is already exceeded")


def _guardrails() -> Guardrails:
    return Guardrails(Policy(), attended=False)


class _FrozenSurface:
    """Always renders the same observation, so "wait" makes no progress --

    the natural way to trigger stuck_no_progress without a browser.
    """

    async def observe(self) -> Observation:
        return Observation(url="http://x/", title="t", nodes=[])

    async def act(self, request) -> ActionOutcome:
        return ActionOutcome(ok=True)


class _AlwaysWaitLLM:
    async def complete_json(self, **kwargs):
        return {
            "action": "wait", "ref": None, "value": None,
            "param_name": None, "output_name": None, "reason": "waiting",
        }


@pytest.mark.asyncio
async def test_max_steps_returns_immediately_without_escalating(tmp_path):
    """Regression test: escalating on "max_steps"/"timeout" achieved nothing --

    resuming from a human intervention doesn't reset step_index or the
    run's elapsed time, so the very next loop iteration re-hits the
    identical stuck_reason and burns another escalation without the model
    ever getting a turn in between. Verified live before this fix: with a
    controller given, `max_steps=0` fired MAX_DISCOVERY_ESCALATIONS human
    interruptions back-to-back for a bound no intervention could clear,
    then gave up -- the escalation_controller argument being present or
    absent should never change *whether* discovery gets stuck on this kind
    of bound, only whether a recoverable obstacle gets a retry.
    """
    settings = Settings(max_steps=0)  # already exceeded before the first iteration
    writer = EvidenceWriter(tmp_path / "run")
    controller = _FakeEscalationController()
    try:
        trace, stopped_reason = await _run_loop(
            object(), _NeverCalledLLM(), _guardrails(), "goal", settings, writer, "run-1",
            escalation_controller=controller,
        )
    finally:
        writer.close()

    assert stopped_reason == "max_steps"
    assert trace == []
    assert controller.calls == 0


@pytest.mark.asyncio
async def test_timeout_returns_immediately_without_escalating(tmp_path):
    settings = Settings(run_timeout_s=0.0)
    writer = EvidenceWriter(tmp_path / "run")
    controller = _FakeEscalationController()
    try:
        trace, stopped_reason = await _run_loop(
            object(), _NeverCalledLLM(), _guardrails(), "goal", settings, writer, "run-1",
            escalation_controller=controller,
        )
    finally:
        writer.close()

    assert stopped_reason == "timeout"
    assert controller.calls == 0


@pytest.mark.asyncio
async def test_max_steps_without_a_controller_also_returns_immediately(tmp_path):
    """Same bound, no controller given at all (e.g. a headless unattended

    discovery run) -- must behave the same as the controller-present case,
    just without ever touching one.
    """
    settings = Settings(max_steps=0)
    writer = EvidenceWriter(tmp_path / "run")
    try:
        trace, stopped_reason = await _run_loop(
            object(), _NeverCalledLLM(), _guardrails(), "goal", settings, writer, "run-1",
            escalation_controller=None,
        )
    finally:
        writer.close()

    assert stopped_reason == "max_steps"
    assert trace == []


@pytest.mark.asyncio
async def test_stuck_no_progress_still_escalates_and_gets_a_real_retry(tmp_path):
    """The fix must not throw out the recoverable case along with the

    pointless one: a genuinely stuck-but-clearable condition still gets up
    to MAX_DISCOVERY_ESCALATIONS real human interruptions, each one giving
    the model a fresh run of max_consecutive_no_progress turns (the reset
    counters), before finally giving up.
    """
    settings = Settings(max_consecutive_no_progress=3, max_steps=1000, run_timeout_s=1000)
    writer = EvidenceWriter(tmp_path / "run")
    controller = _FakeEscalationController()
    try:
        trace, stopped_reason = await _run_loop(
            _FrozenSurface(), _AlwaysWaitLLM(), _guardrails(), "goal", settings, writer, "run-1",
            escalation_controller=controller,
        )
    finally:
        writer.close()

    assert stopped_reason == "stuck_no_progress"
    assert controller.calls == MAX_DISCOVERY_ESCALATIONS
