"""Tests for grip/guardrails.py -- a named test priority in CLAUDE.md.

Constructs Policy directly (not via Policy.load()) so these stay correct
independent of whatever policy.yaml says at any given time.
"""

from __future__ import annotations

import pytest

from grip.config import Policy
from grip.guardrails import GuardedSurface, Guardrails
from grip.surface.base import ActionOutcome, ActionRequest, AXNode, Observation, Surface


def make_policy(**overrides) -> Policy:
    defaults = dict(
        allowed_origins=["http://127.0.0.1:8800"],
        allowed_route_patterns=[r"^/t/[a-z]+/(lookup|member/.*)$"],
        denied_route_patterns=[r"^/_faults"],
        allowed_actions=["click", "type", "select", "navigate", "read", "wait"],
        risky_actions=[],
        risky_control_patterns=[r"\bpost\s+transaction\b"],
        forbidden_control_patterns=[r"\bwire\b"],
        unattended_risky_disposition="escalate",
        sensitive_field_patterns=[r"\bpin\b"],
    )
    defaults.update(overrides)
    return Policy(**defaults)


def node(name: str, role: str = "button", ref: str = "n0") -> AXNode:
    return AXNode(ref=ref, role=role, name=name)


# ---- Guardrails.check() -----------------------------------------------


def test_safe_action_allowed():
    g = Guardrails(make_policy())
    decision = g.check("click", node("Search"), "http://127.0.0.1:8800/t/pinnacle/lookup")
    assert decision.verdict == "allow"
    assert decision.risk == "safe"


def test_disallowed_action_type_blocked():
    g = Guardrails(make_policy(allowed_actions=["click", "navigate"]))
    decision = g.check("type", node("Member #:", role="textbox"), "http://127.0.0.1:8800/t/pinnacle/lookup")
    assert decision.verdict == "block"
    assert "not in allowed_actions" in decision.reason


def test_url_outside_allowlist_blocked():
    g = Guardrails(make_policy())
    decision = g.check("click", node("Search"), "http://127.0.0.1:8800/_faults/interstitial/arm")
    assert decision.verdict == "block"


def test_forbidden_control_blocked_even_when_attended():
    g = Guardrails(make_policy(), attended=True)
    decision = g.check("click", node("Initiate Wire"), "http://127.0.0.1:8800/t/pinnacle/lookup")
    assert decision.verdict == "block"
    assert decision.risk == "forbidden"


def test_risky_control_unattended_escalates_by_default():
    g = Guardrails(make_policy())
    decision = g.check("click", node("Post Transaction"), "http://127.0.0.1:8800/t/pinnacle/lookup")
    assert decision.verdict == "escalate"
    assert decision.risk == "risky"


def test_risky_control_unattended_blocks_when_disposition_is_block():
    g = Guardrails(make_policy(unattended_risky_disposition="block"))
    decision = g.check("click", node("Post Transaction"), "http://127.0.0.1:8800/t/pinnacle/lookup")
    assert decision.verdict == "block"


def test_risky_control_unattended_flags_and_allows_when_disposition_is_flag():
    g = Guardrails(make_policy(unattended_risky_disposition="flag"))
    decision = g.check("click", node("Post Transaction"), "http://127.0.0.1:8800/t/pinnacle/lookup")
    assert decision.verdict == "allow"
    assert decision.risk == "risky"


def test_risky_control_allowed_under_live_attended_supervision():
    g = Guardrails(make_policy(), attended=True)
    decision = g.check("click", node("Post Transaction"), "http://127.0.0.1:8800/t/pinnacle/lookup")
    assert decision.verdict == "allow"
    assert decision.risk == "risky"


def test_redact_matches_sensitive_field_pattern():
    g = Guardrails(make_policy())
    assert g.redact("PIN:", "1234") == "***REDACTED***"
    assert g.redact("Member #:", "12345") == "12345"
    assert g.redact("PIN:", None) is None


# ---- GuardedSurface -----------------------------------------------------


class FakeSurface(Surface):
    """Records every act() call it actually receives, so tests can assert

    a blocked/escalated action never reaches the underlying surface.
    """

    kind = "fake"

    def __init__(self, url: str = "http://127.0.0.1:8800/t/pinnacle/lookup") -> None:
        self.acted: list[ActionRequest] = []
        self._url = url
        self.observation = Observation(
            url=url,
            title="t",
            nodes=[node("Search", ref="n0"), node("Post Transaction", ref="n1")],
        )

    async def observe(self) -> Observation:
        return self.observation

    async def act(self, request: ActionRequest) -> ActionOutcome:
        self.acted.append(request)
        return ActionOutcome(ok=True)

    async def resolve(self, locator):
        return "n0", "primary"

    async def screenshot(self, path: str) -> bool:
        return True

    async def current_url(self) -> str:
        return self._url

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_guarded_surface_blocks_before_reaching_wrapped_surface():
    fake = FakeSurface()
    guarded = GuardedSurface(fake, Guardrails(make_policy()))
    await guarded.observe()

    request = ActionRequest(action="click", ref="n1")  # "Post Transaction" node
    outcome = await guarded.act(request)

    assert outcome.ok is False
    assert outcome.error_kind == "escalation_required"  # distinct from a plain policy block
    assert "escalation required" in outcome.detail
    assert fake.acted == []  # never reached the real surface


@pytest.mark.asyncio
async def test_guarded_surface_allows_safe_action_through():
    fake = FakeSurface()
    guarded = GuardedSurface(fake, Guardrails(make_policy()))
    await guarded.observe()

    request = ActionRequest(action="click", ref="n0")  # "Search" node
    outcome = await guarded.act(request)

    assert outcome.ok is True
    assert len(fake.acted) == 1


@pytest.mark.asyncio
async def test_guarded_surface_checks_current_url_for_non_navigate_actions():
    """A caller cannot claim to be somewhere it isn't -- the guarded surface

    asks the wrapped surface for the real current URL rather than trusting
    anything the caller's ActionRequest itself might imply.
    """
    fake = FakeSurface(url="http://127.0.0.1:8800/_faults/panel")
    guarded = GuardedSurface(fake, Guardrails(make_policy()))
    await guarded.observe()

    outcome = await guarded.act(ActionRequest(action="click", ref="n0"))

    assert outcome.ok is False
    assert fake.acted == []
