"""Thin enforcement layer over ``Policy`` (design rule 4 in CLAUDE.md).

The policy file is the reviewable contract; this module is what makes it
impossible to bypass. ``GuardedSurface`` wraps a real ``Surface`` and is the
*only* thing discovery and replay are ever handed a reference to -- neither
ever sees the raw ``PlaywrightSurface``. That is a structural guarantee, not
a convention: there is no code path from an agent loop or the replay engine
to the browser that does not pass through ``check()`` first, because there
is no other object exposing ``act()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from keystone.config import Policy, RiskClass
from keystone.evidence import REDACTED
from keystone.schemas import AXLocator
from keystone.surface.base import ActionOutcome, ActionRequest, AXNode, Observation, Surface

Verdict = Literal["allow", "block", "escalate"]


@dataclass
class GuardrailDecision:
    verdict: Verdict
    reason: str
    risk: RiskClass = "safe"

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"


class Guardrails:
    """Evaluates one action against the policy. Holds no surface state."""

    def __init__(self, policy: Policy, *, attended: bool = False) -> None:
        self._policy = policy
        self.attended = attended
        """Whether a human is actively watching this run right now (the

        escalation/handoff live-session case, Phase 6). A risky action that
        would otherwise escalate is allowed when attended, on the theory
        that a human watching the browser *is* the confirmation -- but a
        forbidden control is never allowed regardless.
        """

    def check(self, action: str, target_node: AXNode | None, url: str | None) -> GuardrailDecision:
        """The one gate every action passes through. Checked in this order:

        1. Is this action *type* permitted at all (``allowed_actions``)?
        2. Is the current/destination URL in bounds (default-deny origin,
           denied routes take precedence over allowed ones)?
        3. Is the (action, control) pair classified safe, risky, or
           forbidden? Forbidden always blocks. Risky is gated by
           ``unattended_risky_disposition`` unless a human is attending.
        """
        allowed, reason = self._policy.action_allowed(action)
        if not allowed:
            return GuardrailDecision("block", reason)

        if url is not None:
            allowed, reason = self._policy.url_allowed(url)
            if not allowed:
                return GuardrailDecision("block", reason)

        control_name = target_node.name if target_node else None
        risk = self._policy.classify(action, control_name)

        if risk == "forbidden":
            return GuardrailDecision(
                "block", f"control {control_name!r} matches a forbidden pattern", risk
            )

        if risk == "risky":
            if self.attended:
                return GuardrailDecision(
                    "allow", f"risky control {control_name!r} allowed under live supervision", risk
                )
            disposition = self._policy.unattended_risky_disposition
            if disposition == "block":
                return GuardrailDecision(
                    "block", f"risky control {control_name!r} blocked (unattended)", risk
                )
            if disposition == "escalate":
                return GuardrailDecision(
                    "escalate",
                    f"risky control {control_name!r} requires human confirmation (unattended)",
                    risk,
                )
            # "flag": proceeds, but the caller is expected to record `risk`
            # in the evidence trace rather than silently drop it.
            return GuardrailDecision(
                "allow", f"risky control {control_name!r} flagged, proceeding (unattended)", risk
            )

        return GuardrailDecision("allow", "ok", risk)

    def redact(self, control_name: str | None, value: str | None) -> str | None:
        """Mirrors ``AXLocator``/evidence redaction for anything guardrails

        itself surfaces (e.g. a decision reason echoing a typed value). The
        canonical redaction point for evidence records is still their own
        constructor (see keystone/evidence.py) -- this exists so a guardrail
        decision never becomes the leak the record redaction was meant to
        prevent.
        """
        if value is None:
            return None
        return REDACTED if self._policy.is_sensitive_field(control_name) else value

    def is_sensitive_field(self, control_name: str | None) -> bool:
        """Passthrough so callers (discovery's evidence recording) can decide

        whether to mark a value sensitive without reaching past Guardrails
        into the Policy object it wraps.
        """
        return self._policy.is_sensitive_field(control_name)


class GuardedSurface(Surface):
    """Wraps a ``Surface`` so every action is checked before it reaches it.

    Perception (``observe``/``resolve``/``screenshot``/``current_url``) is
    passed through unchanged -- looking is never unsafe, only acting is.
    """

    kind = "guarded"

    def __init__(self, surface: Surface, guardrails: Guardrails) -> None:
        self._surface = surface
        self._guardrails = guardrails
        self._last_observation: Observation | None = None

    async def observe(self) -> Observation:
        observation = await self._surface.observe()
        self._last_observation = observation
        return observation

    def _target_node(self, request: ActionRequest) -> AXNode | None:
        # A replay locator's recorded name is authoritative and doesn't
        # depend on a cached observation staying in sync with whatever
        # resolve() most recently scanned -- prefer it when present.
        if request.locator is not None:
            return AXNode(ref="", role=request.locator.role, name=request.locator.name)
        if request.ref and self._last_observation is not None:
            return self._last_observation.by_ref(request.ref)
        return None

    async def act(self, request: ActionRequest) -> ActionOutcome:
        if request.action == "navigate":
            url = request.url
        else:
            # Checked against the surface's actual current URL, not a
            # caller-supplied one -- a caller cannot claim to be somewhere
            # it isn't in order to slip an action past the allowlist.
            url = await self._surface.current_url()

        target_node = self._target_node(request)
        decision = self._guardrails.check(request.action, target_node, url)

        if decision.verdict == "block":
            return ActionOutcome(
                ok=False, detail=f"blocked by policy: {decision.reason}", error_kind="disabled"
            )
        if decision.verdict == "escalate":
            return ActionOutcome(
                ok=False,
                detail=f"escalation required: {decision.reason}",
                error_kind="escalation_required",
            )

        return await self._surface.act(request)

    async def resolve(self, locator: AXLocator) -> tuple[str | None, str]:
        return await self._surface.resolve(locator)

    async def screenshot(self, path: str) -> bool:
        return await self._surface.screenshot(path)

    async def current_url(self) -> str:
        return await self._surface.current_url()

    async def close(self) -> None:
        await self._surface.close()
