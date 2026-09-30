"""Pure unit tests for keystone/discovery/compiler.py -- no browser, no LLM,

no API key. compile_artifact() is a deterministic transformation over an
already-recorded trace, so it can be tested with a synthetic one.
"""

from __future__ import annotations

from keystone.config import Policy
from keystone.discovery.compiler import DiscoveredStep, compile_artifact
from keystone.schemas import AXLocator


def locator(role: str, name: str) -> AXLocator:
    return AXLocator(role=role, name=name, name_match="normalized", frame_path=[], ordinal=0)


def make_policy(**overrides) -> Policy:
    defaults = dict(
        allowed_origins=["http://127.0.0.1:8800"],
        risky_control_patterns=[r"\bpost\s+transaction\b"],
        sensitive_field_patterns=[r"\bpin\b"],
    )
    defaults.update(overrides)
    return Policy(**defaults)


def happy_path_trace() -> list[DiscoveredStep]:
    return [
        DiscoveredStep(
            index=0, action="type", locator=locator("textbox", "Member #:"),
            value="12345", param_name="member_id", reason="type the member number",
        ),
        DiscoveredStep(
            index=1, action="click", locator=locator("button", "Search"),
            reason="submit the search",
        ),
        DiscoveredStep(
            index=2, action="read", locator=locator("cell", "Regular Savings Balance:"),
            output_name="regular_savings_balance", read_value="4,182.55",
            reason="read the balance",
        ),
    ]


def test_type_step_becomes_a_param_with_substituted_checkpoint():
    artifact = compile_artifact(
        capability_id="lookup_member_savings_balance", goal="look up member 12345 and read their balance",
        entry_url="http://127.0.0.1:8800/t/pinnacle/lookup", tenant=None,
        trace=happy_path_trace(), policy=make_policy(),
        discovery_run_id="r1", model="test-model", model_base_url="http://x",
    )
    assert [p.name for p in artifact.params] == ["member_id"]
    assert artifact.params[0].example == "12345"

    type_step = artifact.steps[0]
    assert type_step.value.kind == "param"
    assert type_step.value.param == "member_id"
    assert type_step.checkpoint.kind == "ax_value_matches"
    assert type_step.checkpoint.pattern == "^${member_id}$"


def test_click_checkpoint_uses_next_steps_target_not_url():
    artifact = compile_artifact(
        capability_id="cap", goal="g", entry_url="http://x/lookup", tenant=None,
        trace=happy_path_trace(), policy=make_policy(),
        discovery_run_id="r1", model="m", model_base_url="http://x",
    )
    click_step = artifact.steps[1]
    assert click_step.checkpoint.kind == "ax_present"
    assert click_step.checkpoint.locator.name == "Regular Savings Balance:"


def test_read_step_becomes_output_and_success_condition():
    artifact = compile_artifact(
        capability_id="cap", goal="g", entry_url="http://x/lookup", tenant=None,
        trace=happy_path_trace(), policy=make_policy(),
        discovery_run_id="r1", model="m", model_base_url="http://x",
    )
    assert len(artifact.outputs) == 1
    assert artifact.outputs[0].name == "regular_savings_balance"
    assert artifact.outputs[0].source.locator.name == "Regular Savings Balance:"

    assert artifact.success.kind == "ax_present"
    assert artifact.success.locator.name == "Regular Savings Balance:"

    read_step = artifact.steps[2]
    assert read_step.captures == "regular_savings_balance"
    assert read_step.checkpoint is None  # step 1's checkpoint already covers this


def test_sensitive_field_always_becomes_a_param_even_without_a_tag():
    trace = [
        DiscoveredStep(
            index=0, action="type", locator=locator("textbox", "PIN:"),
            value="1234", param_name=None, sensitive=True, reason="enter pin",
        ),
    ]
    artifact = compile_artifact(
        capability_id="cap", goal="g", entry_url="http://x/lookup", tenant=None,
        trace=trace, policy=make_policy(),
        discovery_run_id="r1", model="m", model_base_url="http://x",
    )
    assert len(artifact.params) == 1
    assert artifact.params[0].sensitive is True
    assert artifact.params[0].example is None  # never persist the raw value
    assert artifact.steps[0].value.kind == "param"


def test_literal_value_with_no_param_name_stays_a_literal():
    trace = [
        DiscoveredStep(
            index=0, action="select", locator=locator("combobox", "Branch:"),
            value="All Branches", param_name=None, reason="leave branch as default",
        ),
    ]
    artifact = compile_artifact(
        capability_id="cap", goal="g", entry_url="http://x/lookup", tenant=None,
        trace=trace, policy=make_policy(),
        discovery_run_id="r1", model="m", model_base_url="http://x",
    )
    assert artifact.params == []
    assert artifact.steps[0].value.kind == "literal"
    assert artifact.steps[0].value.value == "All Branches"


def test_risk_is_classified_from_the_real_policy_not_hardcoded_safe():
    trace = [
        DiscoveredStep(index=0, action="click", locator=locator("button", "Post Transaction"), reason="post it"),
    ]
    artifact = compile_artifact(
        capability_id="cap", goal="g", entry_url="http://x/lookup", tenant=None,
        trace=trace, policy=make_policy(),
        discovery_run_id="r1", model="m", model_base_url="http://x",
    )
    assert artifact.steps[0].risk == "risky"


def test_no_read_steps_falls_back_to_a_degenerate_success_condition():
    trace = [
        DiscoveredStep(index=0, action="click", locator=locator("button", "Continue"), reason="proceed"),
    ]
    artifact = compile_artifact(
        capability_id="cap", goal="g", entry_url="http://x/lookup", tenant=None,
        trace=trace, policy=make_policy(),
        discovery_run_id="r1", model="m", model_base_url="http://x",
    )
    assert artifact.outputs == []
    assert artifact.success.kind == "url_matches"


def test_compiled_artifact_is_a_draft_with_provenance_recorded():
    artifact = compile_artifact(
        capability_id="cap", goal="g", entry_url="http://x/lookup", tenant="pinnacle",
        trace=happy_path_trace(), policy=make_policy(),
        discovery_run_id="run-123", model="openai/gpt-oss-20b", model_base_url="http://x",
    )
    assert artifact.approval.state == "draft"
    assert artifact.provenance.discovery_run_id == "run-123"
    assert artifact.provenance.model == "openai/gpt-oss-20b"
    assert artifact.provenance.steps_explored == 3
