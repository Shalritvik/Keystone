"""Tests for grip/reliability.py against the real mock app -- the whole

point of this module is measuring genuine replay behaviour, so a fake
surface would just test that the arithmetic is right, not that the signal
means anything.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from grip.config import Policy
from grip.guardrails import GuardedSurface, Guardrails
from grip.reliability import assess
from grip.schemas import CapabilityArtifact
from grip.surface.web import PlaywrightSurface

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT_PATH = REPO_ROOT / "artifacts" / "lookup_member_savings_balance.v1.json"


@pytest.fixture
def artifact() -> CapabilityArtifact:
    return CapabilityArtifact.model_validate(json.loads(ARTIFACT_PATH.read_text()))


def make_factory(policy: Policy):
    async def factory() -> GuardedSurface:
        raw = await PlaywrightSurface.create(headless=True)
        return GuardedSurface(raw, Guardrails(policy, attended=False))

    return factory


@pytest.mark.asyncio
async def test_clean_artifact_is_perfectly_healthy(clear_faults, tmp_path, artifact):
    policy = Policy.load(REPO_ROOT / "policy.yaml")
    report = await assess(make_factory(policy), tmp_path, artifact, {"member_id": "12345"}, n_runs=3)

    assert report.runs == 3
    assert report.pass_rate == 1.0
    assert report.primary_resolution_rate == 1.0
    assert report.is_healthy is True
    assert report.statuses == ["success", "success", "success"]
    assert len(set(report.run_ids)) == 3  # a fresh run_id (and browser context) every time


@pytest.mark.asyncio
async def test_an_injected_failure_makes_the_report_unhealthy(mockapp_server, tmp_path, artifact):
    httpx.post(f"{mockapp_server}/_faults/server_error/arm", params={"times": 1})
    policy = Policy.load(REPO_ROOT / "policy.yaml")
    report = await assess(make_factory(policy), tmp_path, artifact, {"member_id": "12345"}, n_runs=2)

    assert report.runs == 2
    assert report.failures == 1
    assert report.pass_rate == 0.5
    assert report.is_healthy is False
    httpx.post(f"{mockapp_server}/_faults/clear")


def test_report_with_no_runs_is_not_healthy():
    from grip.reliability import ReliabilityReport

    report = ReliabilityReport()
    assert report.is_healthy is False  # never silently "healthy" by vacuous default
    assert report.pass_rate == 0.0
    assert report.primary_resolution_rate == 1.0  # no resolutions attempted, nothing to be unhealthy about
