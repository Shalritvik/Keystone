"""Tests for grip/schemas.py's validators and the artifact contract itself --

named first in CLAUDE.md's testing priorities. Pure unit tests, no browser,
no API key: these are pydantic model shape/behaviour checks.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from grip.schemas import (
    AXLocator,
    CapabilityArtifact,
    Condition,
    Extraction,
    OutputSpec,
    ParamSpec,
    RecoveryRule,
    ReplayResult,
    Step,
    StepTrace,
    SurfaceBinding,
    TenantOverride,
    ValueSource,
)


def locator(name: str = "Member #:", role: str = "textbox", ordinal: int = 0) -> AXLocator:
    return AXLocator(role=role, name=name, name_match="normalized", frame_path=[], ordinal=ordinal)


# ---- Condition shape validation -----------------------------------------


def test_ax_present_requires_a_locator():
    with pytest.raises(ValidationError):
        Condition(kind="ax_present")


def test_url_matches_requires_a_pattern():
    with pytest.raises(ValidationError):
        Condition(kind="url_matches")


def test_ax_value_matches_requires_both_locator_and_pattern():
    with pytest.raises(ValidationError):
        Condition(kind="ax_value_matches", locator=locator())
    with pytest.raises(ValidationError):
        Condition(kind="ax_value_matches", pattern="x")
    # both present: fine
    Condition(kind="ax_value_matches", locator=locator(), pattern="x")


def test_text_present_and_absent_require_a_pattern():
    with pytest.raises(ValidationError):
        Condition(kind="text_present")
    Condition(kind="text_present", pattern="ok")


def test_ax_absent_needs_no_pattern():
    Condition(kind="ax_absent", locator=locator())  # must not raise


# ---- ValueSource shape validation -----------------------------------------


def test_value_source_literal_requires_value():
    with pytest.raises(ValidationError):
        ValueSource(kind="literal")
    ValueSource(kind="literal", value="x")


def test_value_source_param_requires_param_name():
    with pytest.raises(ValidationError):
        ValueSource(kind="param")
    ValueSource(kind="param", param="member_id")


def test_value_source_extracted_requires_from_output():
    with pytest.raises(ValidationError):
        ValueSource(kind="extracted")
    ValueSource(kind="extracted", from_output="balance")


def test_value_source_literal_allows_empty_string():
    """An intentional empty-string literal (e.g. "clear this field") is

    distinct from no value at all -- see the RecoveryRule bug this
    distinction mattered for in grip/replay/engine.py.
    """
    vs = ValueSource(kind="literal", value="")
    assert vs.value == ""


# ---- Step shape validation -----------------------------------------


def test_click_and_select_require_a_target():
    with pytest.raises(ValidationError):
        Step(index=0, action="click")
    with pytest.raises(ValidationError):
        Step(index=0, action="select", target=None)
    Step(index=0, action="click", target=locator())


def test_type_requires_target_and_value():
    with pytest.raises(ValidationError):
        Step(index=0, action="type", target=locator())  # missing value
    with pytest.raises(ValidationError):
        Step(index=0, action="type", value=ValueSource(kind="literal", value="x"))  # missing target
    Step(index=0, action="type", target=locator(), value=ValueSource(kind="literal", value="x"))


def test_navigate_requires_route_template_or_value():
    with pytest.raises(ValidationError):
        Step(index=0, action="navigate")
    Step(index=0, action="navigate", route_template="/lookup")
    Step(index=0, action="navigate", value=ValueSource(kind="literal", value="http://x/"))


def test_read_requires_target_and_captures():
    with pytest.raises(ValidationError):
        Step(index=0, action="read", target=locator())  # missing captures
    with pytest.raises(ValidationError):
        Step(index=0, action="read", captures="balance")  # missing target
    Step(index=0, action="read", target=locator(), captures="balance")


def test_wait_needs_neither_target_nor_value():
    Step(index=0, action="wait")  # must not raise


# ---- RecoveryRule -----------------------------------------


def test_recovery_rule_click_type_select_require_a_target():
    with pytest.raises(ValidationError):
        RecoveryRule(
            code="X", description="d",
            detect=Condition(kind="ax_present", locator=locator()),
            action="click",
        )
    RecoveryRule(
        code="X", description="d",
        detect=Condition(kind="ax_present", locator=locator()),
        action="click", target=locator("Continue", role="button"),
    )


def test_recovery_rule_wait_needs_no_target():
    RecoveryRule(
        code="X", description="d",
        detect=Condition(kind="ax_present", locator=locator()),
        action="wait",
    )


# ---- AXLocator.identity_key -----------------------------------------


def test_identity_key_ignores_rationale_and_fallbacks():
    a = locator()
    a.rationale = "reason A"
    b = locator()
    b.rationale = "a completely different reason B"
    b.fallbacks = [{"strategy": "css", "expression": "#x", "weaker_because": "w"}]
    assert a.identity_key() == b.identity_key()


def test_identity_key_distinguishes_role_name_ordinal():
    base = locator()
    assert base.identity_key() != locator(role="button").identity_key()
    assert base.identity_key() != locator(name="Other").identity_key()
    assert base.identity_key() != locator(ordinal=1).identity_key()


# ---- CapabilityArtifact: hash, seal, tenant override, tool schema --------


def make_artifact(**overrides) -> CapabilityArtifact:
    defaults = dict(
        capability_id="cap", version=1, title="t", description="d", goal="g",
        surface=SurfaceBinding(kind="web", entry="http://x/lookup"),
        params=[ParamSpec(name="member_id", required=True, pattern=r"^\d+$", description="id")],
        outputs=[
            OutputSpec(
                name="balance", required=True,
                source=Extraction(locator=locator("Balance:", role="cell"), attribute="value"),
            )
        ],
        steps=[Step(index=0, action="type", target=locator(), value=ValueSource(kind="param", param="member_id"))],
        success=Condition(kind="ax_present", locator=locator()),
    )
    defaults.update(overrides)
    return CapabilityArtifact(**defaults)


def test_compute_hash_is_deterministic():
    a1 = make_artifact()
    a2 = make_artifact()
    assert a1.compute_hash() == a2.compute_hash()


def test_compute_hash_changes_when_steps_change():
    a1 = make_artifact()
    a2 = make_artifact(steps=[Step(index=0, action="wait")])
    assert a1.compute_hash() != a2.compute_hash()


def test_compute_hash_ignores_provenance_and_approval():
    """"Approving an artifact or re-recording it at a later date does not

    change its identity" -- the docstring's own claim, checked directly.
    """
    a1 = make_artifact()
    hash_before = a1.compute_hash()
    a1.approval.state = "approved"
    a1.approval.approved_by = "someone"
    a1.provenance.model = "some-model"
    assert a1.compute_hash() == hash_before


def test_seal_sets_provenance_content_hash():
    a = make_artifact()
    assert a.provenance.content_hash == ""
    a.seal()
    assert a.provenance.content_hash == a.compute_hash()
    assert len(a.provenance.content_hash) == 16


def test_for_tenant_with_no_override_returns_same_object():
    a = make_artifact()
    assert a.for_tenant("nonexistent_tenant") is a
    assert a.for_tenant(None) is a


def test_for_tenant_disabled_steps_are_removed():
    a = make_artifact(
        steps=[
            Step(index=0, action="type", target=locator(), value=ValueSource(kind="param", param="member_id")),
            Step(index=1, action="click", target=locator("Search", role="button")),
        ],
        tenant_overrides=[TenantOverride(tenant="harbor", disabled_steps=[1])],
    )
    clone = a.for_tenant("harbor")
    assert [s.index for s in clone.steps] == [0]
    assert [s.index for s in a.steps] == [0, 1]  # original untouched


def test_for_tenant_patches_matching_checkpoint_locator_too():
    """Regression coverage for the bug found verifying cross-tenant replay

    live: overriding a step's target must also patch that step's own
    checkpoint when the checkpoint targets the *same* control, or the
    override only half-applies.
    """
    field = locator("Member #:")
    step = Step(
        index=0, action="type", target=field, value=ValueSource(kind="param", param="member_id"),
        checkpoint=Condition(kind="ax_value_matches", locator=locator("Member #:"), pattern="x"),
    )
    override_target = locator("Account Number:")
    a = make_artifact(
        steps=[step],
        tenant_overrides=[TenantOverride(tenant="harbor", step_targets={0: override_target})],
    )
    clone = a.for_tenant("harbor")
    assert clone.steps[0].target.name == "Account Number:"
    assert clone.steps[0].checkpoint.locator.name == "Account Number:"
    # Original is untouched.
    assert a.steps[0].target.name == "Member #:"
    assert a.steps[0].checkpoint.locator.name == "Member #:"


def test_to_tool_schema_shape():
    schema = make_artifact().to_tool_schema()
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == "cap"
    props = fn["parameters"]["properties"]
    assert "member_id" in props
    assert props["member_id"]["pattern"] == r"^\d+$"
    assert fn["parameters"]["required"] == ["member_id"]


def test_to_tool_schema_omits_non_required_params_from_required_list():
    a = make_artifact(params=[ParamSpec(name="opt", required=False)])
    schema = a.to_tool_schema()
    assert schema["function"]["parameters"]["required"] == []


def test_to_tool_schema_surfaces_the_example_value():
    """Regression test: ParamSpec.example was declared and set on every

    real artifact, but to_tool_schema() -- the method whose own docstring
    calls it "the whole point of typing the artifact" -- never read it, so
    a calling agent never saw the sample value an artifact author wrote.
    """
    a = make_artifact(params=[ParamSpec(name="member_id", example="12345")])
    props = a.to_tool_schema()["function"]["parameters"]["properties"]
    assert props["member_id"]["example"] == "12345"


def test_to_tool_schema_omits_example_key_when_unset():
    a = make_artifact(params=[ParamSpec(name="member_id")])
    props = a.to_tool_schema()["function"]["parameters"]["properties"]
    assert "example" not in props["member_id"]


# ---- ReplayResult.ok -----------------------------------------


@pytest.mark.parametrize(
    "status,expected_ok",
    [("success", True), ("business", True), ("failure", False), ("escalated", False)],
)
def test_replay_result_ok_reflects_usable_answer_not_just_success(status, expected_ok):
    result = ReplayResult(status=status, capability_id="cap", capability_version=1, steps=[])
    assert result.ok is expected_ok


def test_step_trace_resolved_field_defaults_to_none():
    trace = StepTrace(index=0, action="click", status="ok")
    assert trace.resolved is None
