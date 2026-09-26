"""End-to-end replay tests against the real mock app -- no API key needed,

which is the entire point of replay having no model in it (CLAUDE.md).
Covers the error taxonomy's three branches (success / business / failure),
recovery, and cross-tenant reuse via the hand-written Phase 4 artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from grip.replay.engine import ReplayEngine
from grip.schemas import CapabilityArtifact

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT_PATH = REPO_ROOT / "artifacts" / "lookup_member_savings_balance.v1.json"


@pytest.fixture
def artifact() -> CapabilityArtifact:
    return CapabilityArtifact.model_validate(json.loads(ARTIFACT_PATH.read_text()))


@pytest.fixture
def engine(guarded_surface, tmp_path) -> ReplayEngine:
    return ReplayEngine(guarded_surface, tmp_path)


def test_artifact_file_is_valid_against_the_schema():
    CapabilityArtifact.model_validate(json.loads(ARTIFACT_PATH.read_text()))


@pytest.mark.asyncio
async def test_success_branch(engine, artifact):
    result = await engine.run(artifact, {"member_id": "12345"})
    assert result.status == "success"
    assert result.ok is True
    assert result.outputs["regular_savings_balance"] == "4,182.55"
    assert [s.status for s in result.steps] == ["ok", "ok", "ok", "ok"]
    # Every locator on this clean artifact resolves via primary match --
    # the raw material grip/reliability.py reads back to score a run.
    assert [s.resolved for s in result.steps[1:]] == ["primary", "primary", "primary"]


@pytest.mark.asyncio
async def test_business_branch_member_not_found(engine, artifact):
    result = await engine.run(artifact, {"member_id": "99999"})
    assert result.status == "business"
    assert result.ok is True  # a business outcome is a usable answer, not an error
    assert result.outcome_code == "MEMBER_NOT_FOUND"
    assert result.outputs == {}


@pytest.mark.asyncio
async def test_business_branch_permission_denied(engine, artifact):
    result = await engine.run(artifact, {"member_id": "44120"})
    assert result.status == "business"
    assert result.ok is True
    assert result.outcome_code == "PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_failure_branch_on_injected_server_error(engine, artifact, mockapp_server):
    httpx.post(f"{mockapp_server}/_faults/server_error/arm")
    result = await engine.run(artifact, {"member_id": "12345"})
    assert result.status == "failure"
    assert result.ok is False
    assert result.failure_kind == "checkpoint_failed"
    assert result.failed_step == 1
    assert result.expected is not None
    assert result.observed is not None
    # A screenshot was captured for the failing step in the evidence trace.
    run_dir = Path(result.evidence_dir)
    shots = list((run_dir / "screenshots").glob("*.png"))
    assert len(shots) == 1


@pytest.mark.asyncio
async def test_recovers_from_interstitial_and_still_succeeds(engine, artifact, mockapp_server):
    httpx.post(f"{mockapp_server}/_faults/interstitial/arm")
    result = await engine.run(artifact, {"member_id": "22881"})
    assert result.status == "success"
    assert result.outputs["regular_savings_balance"] == "17,640.12"
    assert result.steps[0].status == "recovered"
    assert result.steps[0].recoveries_applied == ["DISMISS_MOTD_INTERSTITIAL"]


@pytest.mark.asyncio
async def test_cross_tenant_replay_with_two_small_overrides(engine, artifact):
    """Harbor renames both the searched field's caption and the submit

    button's caption (mockapp/data.py); the artifact's tenant_overrides
    patches exactly those two, and the field reorder needs no override at
    all -- the accessible-name targeting argument, demonstrated rather than
    asserted.
    """
    result = await engine.run(artifact, {"member_id": "12345"}, tenant="harbor")
    assert result.status == "success"
    assert result.tenant == "harbor"
    assert result.outputs["regular_savings_balance"] == "4,182.55"
    assert result.steps[1].target == 'textbox "Account Number:"'
    assert result.steps[2].target == 'button "Find"'


@pytest.mark.asyncio
async def test_evidence_trace_is_written_and_readable(engine, artifact):
    from grip.evidence import EvidenceReader

    result = await engine.run(artifact, {"member_id": "12345"})
    reader = EvidenceReader(result.evidence_dir)
    assert reader.finished() is True
    events = reader.read_events()
    kinds = [e.kind for e in events]
    assert kinds[0] == "run_started"
    assert kinds[-1] == "run_finished"
    assert kinds.count("step_started") == kinds.count("step_finished") == 4


@pytest.mark.asyncio
async def test_rejects_missing_required_param(engine, artifact):
    from grip.replay.engine import ReplayError

    with pytest.raises(ReplayError):
        await engine.run(artifact, {})


@pytest.mark.asyncio
async def test_rejects_param_violating_its_pattern(engine, artifact):
    from grip.replay.engine import ReplayError

    with pytest.raises(ReplayError):
        await engine.run(artifact, {"member_id": "not-a-number"})


@pytest.mark.asyncio
async def test_rejects_unknown_param(engine, artifact):
    from grip.replay.engine import ReplayError

    with pytest.raises(ReplayError):
        await engine.run(artifact, {"member_id": "12345", "extra": "x"})
