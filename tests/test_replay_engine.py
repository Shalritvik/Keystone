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

from keystone.replay.engine import ReplayEngine
from keystone.schemas import CapabilityArtifact

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
    assert result.outputs["regular_savings_balance"] == {"amount": "4182.55", "currency": "USD"}
    assert [s.status for s in result.steps] == ["ok", "ok", "ok", "ok"]
    # Every locator on this clean artifact resolves via primary match --
    # the raw material keystone/reliability.py reads back to score a run.
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
    assert result.outputs["regular_savings_balance"] == {"amount": "17640.12", "currency": "USD"}
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
    assert result.outputs["regular_savings_balance"] == {"amount": "4182.55", "currency": "USD"}
    assert result.steps[1].target == 'textbox "Account Number:"'
    assert result.steps[2].target == 'button "Find"'


@pytest.mark.asyncio
async def test_evidence_trace_is_written_and_readable(engine, artifact):
    from keystone.evidence import EvidenceReader

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
    from keystone.replay.engine import ReplayError

    with pytest.raises(ReplayError):
        await engine.run(artifact, {})


@pytest.mark.asyncio
async def test_rejects_param_violating_its_pattern(engine, artifact):
    from keystone.replay.engine import ReplayError

    with pytest.raises(ReplayError):
        await engine.run(artifact, {"member_id": "not-a-number"})


@pytest.mark.asyncio
async def test_rejects_unknown_param(engine, artifact):
    from keystone.replay.engine import ReplayError

    with pytest.raises(ReplayError):
        await engine.run(artifact, {"member_id": "12345", "extra": "x"})


@pytest.mark.asyncio
async def test_a_recovery_action_that_itself_fails_is_not_falsely_reported_as_recovered(
    engine, mockapp_server
):
    """Regression test: _try_recover discarded the ActionOutcome of its own

    recovery action, so a recovery whose action itself fails (wrong action
    type for the target, a bad locator that still resolves to *something*,
    etc.) was unconditionally reported as "recovered" -- the caller would
    then retry the original step against an unchanged obstacle, and the
    evidence trail would falsely claim the obstacle was cleared.
    """
    from keystone.schemas import (
        AXLocator, CapabilityArtifact, Condition, RecoveryRule, Step, SurfaceBinding, ValueSource,
    )

    field = AXLocator(role="textbox", name="Member #:", name_match="normalized", frame_path=[], ordinal=0)
    dialog = AXLocator(role="dialog", name="System Message", name_match="exact", frame_path=[], ordinal=0)
    continue_button = AXLocator(role="button", name="Continue", name_match="normalized", frame_path=[], ordinal=0)

    broken_artifact = CapabilityArtifact(
        capability_id="broken_recovery_test", version=1, title="t", description="d", goal="g",
        surface=SurfaceBinding(kind="web", entry=f"{mockapp_server}/t/pinnacle/lookup"),
        steps=[Step(index=0, action="type", target=field, value=ValueSource(kind="literal", value="12345"))],
        success=Condition(kind="ax_present", locator=field, description="field present"),
        recoveries=[
            RecoveryRule(
                code="BROKEN_RECOVERY",
                description="deliberately the wrong action type for its target (select on a button)",
                detect=Condition(kind="ax_present", locator=dialog, description="interstitial present"),
                action="select", target=continue_button, value="x", max_attempts=2,
            )
        ],
    )

    httpx.post(f"{mockapp_server}/_faults/interstitial/arm")
    result = await engine.run(broken_artifact, {})

    assert all(s.recoveries_applied == [] for s in result.steps), (
        "a recovery action that itself failed must never appear in recoveries_applied"
    )


@pytest.mark.asyncio
async def test_required_output_with_non_participating_capture_group_fails_loudly(engine):
    """Regression test: re.search("(X)?", "4,182.55") matches (the group is

    optional) but group(1) is None -- `match` was truthy so the old code
    never reached the "no match" required-check, and a required output
    silently came back None inside a status="success" result. Verified
    live before this fix existed.
    """
    from keystone.schemas import (
        AXLocator, CapabilityArtifact, Condition, Extraction, OutputSpec, Step, SurfaceBinding, ValueSource,
    )

    balance = AXLocator(role="cell", name="Regular Savings Balance:", name_match="normalized", frame_path=[], ordinal=0)
    field = AXLocator(role="textbox", name="Member #:", name_match="normalized", frame_path=[], ordinal=0)
    search = AXLocator(role="button", name="Search", name_match="normalized", frame_path=[], ordinal=0)

    broken_artifact = CapabilityArtifact(
        capability_id="capture_group_test", version=1, title="t", description="d", goal="g",
        surface=SurfaceBinding(kind="web", entry="http://127.0.0.1:8800/t/pinnacle/lookup"),
        steps=[
            Step(index=0, action="type", target=field, value=ValueSource(kind="param", param="member_id")),
            Step(index=1, action="click", target=search),
        ],
        params=[{"name": "member_id", "required": True}],
        outputs=[
            OutputSpec(
                name="regular_savings_balance", required=True,
                source=Extraction(locator=balance, attribute="value", capture_pattern=r"(XYZ)?"),
            )
        ],
        success=Condition(kind="ax_present", locator=balance, description="balance present"),
    )

    result = await engine.run(broken_artifact, {"member_id": "12345"})

    assert result.status == "failure"
    assert result.failure_kind == "output_missing"
    assert result.outputs == {}


@pytest.mark.asyncio
async def test_tenant_override_param_defaults_applies_and_can_be_overridden(engine):
    """Regression test: TenantOverride.param_defaults was declared in the

    schema and documented but never read anywhere -- a tenant author
    setting it would see it silently do nothing.
    """
    from keystone.schemas import AXLocator, CapabilityArtifact, Condition, Step, SurfaceBinding, TenantOverride, ValueSource

    field = AXLocator(role="textbox", name="Member #:", name_match="normalized", frame_path=[], ordinal=0)
    artifact = CapabilityArtifact(
        capability_id="param_defaults_test", version=1, title="t", description="d", goal="g",
        surface=SurfaceBinding(kind="web", entry="http://127.0.0.1:8800/t/pinnacle/lookup"),
        steps=[Step(index=0, action="type", target=field, value=ValueSource(kind="param", param="member_id"))],
        params=[{"name": "member_id", "required": True}],
        success=Condition(kind="ax_present", locator=field, description="field present"),
        tenant_overrides=[TenantOverride(tenant="pinnacle", param_defaults={"member_id": "12345"})],
    )

    no_params = await engine.run(artifact, {}, tenant="pinnacle")
    assert no_params.status == "success"
    assert no_params.steps[1].value == "12345"

    overridden = await engine.run(artifact, {"member_id": "22881"}, tenant="pinnacle")
    assert overridden.status == "success"
    assert overridden.steps[1].value == "22881"


# ---- _cast: "money" output typing -----------------------------------------
#
# Regression coverage: OutputSpec declared type="money" on the real lookup
# artifact, but _cast() had no branch for it at all -- a "typed" output
# degraded to the raw display string ("4,182.55"), comma and all, exactly
# the thing design rule "the model produces a plan, never data" is meant to
# prevent downstream of discovery too. Found via external review.


def test_cast_money_strips_the_thousands_separator_and_splits_currency():
    assert ReplayEngine._cast("4,182.55", "money") == {"amount": "4182.55", "currency": "USD"}


def test_cast_money_keeps_exact_decimal_precision_not_float_rounding():
    # float("17,640.12".replace(",","")) == 17640.12 looks fine printed, but
    # float is binary -- Decimal is the whole point here, not cosmetic.
    from decimal import Decimal
    result = ReplayEngine._cast("17,640.12", "money")
    assert Decimal(result["amount"]) == Decimal("17640.12")
    assert result["amount"] == "17640.12"  # exact string, no float round-trip


def test_cast_money_tolerates_a_leading_dollar_sign():
    assert ReplayEngine._cast("$4182.55", "money") == {"amount": "4182.55", "currency": "USD"}


def test_cast_money_falls_back_to_the_raw_string_when_it_cant_parse():
    assert ReplayEngine._cast("N/A", "money") == "N/A"


def test_cast_money_none_stays_none():
    assert ReplayEngine._cast(None, "money") is None
