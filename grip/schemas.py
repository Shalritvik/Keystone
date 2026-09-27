"""The capability contract.

This module is the focal point of the system. Everything else is machinery
that either *produces* one of these artifacts (discovery) or *consumes* one
(replay). It is deliberately kept in a single file so that a human reviewer —
or a calling agent's author — can read the entire contract in one sitting.

Three ideas shape the schema:

1. **A capability is an API, not a macro.** It has typed inputs, typed
   outputs, and a declared success condition. A calling agent should be able
   to decide whether to invoke it from the schema alone, without reading the
   steps.

2. **The non-happy paths are part of the contract.** A bank's "no such
   member" is a legitimate answer the caller needs, not a failure. The
   artifact therefore declares its known *business outcomes* and its known
   *recoverable conditions* explicitly. Anything left over is a hard failure.
   Conflating these three is the mistake this schema exists to prevent.

3. **Targeting is described, not hard-coded.** A step names a control by what
   it *is* in the accessibility tree — role, accessible name, containment —
   plus an ordered fallback chain and a written rationale. This is what makes
   the same artifact reusable against a differently-skinned instance of the
   same vendor product.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = "1.0"

ActionType = Literal["navigate", "click", "type", "select", "wait", "read"]
RiskClass = Literal["safe", "risky", "forbidden"]
ValueType = Literal["string", "integer", "number", "boolean", "date", "money"]


# ==========================================================================
# Targeting
# ==========================================================================


class SelectorFallback(BaseModel):
    """A last-resort targeting strategy, used only if the AX locator misses.

    Fallbacks are ordered and each one records *why* it is weaker than the
    primary, so a reviewer can see the degradation path rather than guessing.
    """

    strategy: Literal["ax_role_ordinal", "css", "xpath", "text_proximity"]
    expression: str
    weaker_because: str


class AXLocator(BaseModel):
    """Identify a control by its accessibility-tree identity.

    Why the AX tree rather than CSS selectors or pixel coordinates:

    * Legacy enterprise apps have no test IDs and frequently no meaningful
      class names, but browsers still compute roles and accessible names for
      them. The AX tree is the most stable *semantic* surface these apps
      expose.
    * The same representation exists for native desktop applications (macOS
      AX API, Windows UI Automation), so a locator expressed this way is not
      web-specific. This is the seam that lets the artifact schema outlive the
      choice of surface technology.
    * Unlike screenshot coordinates, it survives window resizing, zoom, theme
      changes, and per-tenant restyling — all of which are common when many
      institutions run the same vendor product with different branding.
    """

    role: str = Field(description="AX role, e.g. 'textbox', 'button', 'link', 'cell'.")
    name: str | None = Field(
        default=None, description="Accessible name of the control, as computed by the browser."
    )
    name_match: Literal["exact", "normalized", "contains", "regex"] = "normalized"

    frame_path: list[str] = Field(
        default_factory=list,
        description=(
            "Ordered frame names/ids from the top document down to the frame "
            "containing the control. Non-empty for frameset-era applications, "
            "where the same control name may exist in several frames."
        ),
    )
    ancestor_roles: list[str] = Field(
        default_factory=list,
        description=(
            "Coarse containment chain (outermost first), used to disambiguate "
            "identically-named controls in different regions of a page."
        ),
    )
    ancestor_name: str | None = Field(
        default=None,
        description="Accessible name of the nearest meaningful ancestor (form, table, region).",
    )
    ordinal: int = Field(
        default=0,
        description=(
            "Zero-based index among otherwise-identical matches. Non-zero is a "
            "smell and the rationale should say why it was unavoidable."
        ),
    )

    fallbacks: list[SelectorFallback] = Field(default_factory=list)
    rationale: str = Field(
        default="",
        description=(
            "Why this targeting is expected to survive. Required on every "
            "recorded step; the reviewer reads this, not the raw selector."
        ),
    )

    def normalized_name(self) -> str | None:
        if self.name is None:
            return None
        return " ".join(self.name.split()).strip().casefold()

    def identity_key(self) -> tuple:
        """Fields that identify *which control* this is, excluding rationale

        and fallbacks -- two locators aimed at the same control legitimately
        carry different rationale text (e.g. a step's target vs. that same
        step's checkpoint re-describing why it re-asserts against it), so
        plain equality is the wrong test for "is this the same control."
        """
        return (
            self.role, self.normalized_name(), self.name_match,
            tuple(self.frame_path), tuple(self.ancestor_roles), self.ancestor_name, self.ordinal,
        )

    def describe(self) -> str:
        bits = [self.role]
        if self.name:
            bits.append(f'"{self.name}"')
        if self.ancestor_name:
            bits.append(f"in {self.ancestor_name!r}")
        if self.frame_path:
            bits.append(f"[frame {'/'.join(self.frame_path)}]")
        if self.ordinal:
            bits.append(f"#{self.ordinal}")
        return " ".join(bits)


# ==========================================================================
# Conditions
# ==========================================================================


class Condition(BaseModel):
    """A checkable assertion about surface state.

    Used for three different jobs — step checkpoints, business-outcome
    detection, and recovery triggers — deliberately sharing one type so that
    the detection logic has exactly one implementation.
    """

    kind: Literal[
        "ax_present",
        "ax_absent",
        "ax_value_matches",
        "url_matches",
        "text_present",
        "text_absent",
    ]
    locator: AXLocator | None = None
    pattern: str | None = None
    description: str = ""

    @model_validator(mode="after")
    def _check_shape(self) -> "Condition":
        needs_locator = {"ax_present", "ax_absent", "ax_value_matches"}
        needs_pattern = {"ax_value_matches", "url_matches", "text_present", "text_absent"}
        if self.kind in needs_locator and self.locator is None:
            raise ValueError(f"condition kind {self.kind!r} requires a locator")
        if self.kind in needs_pattern and not self.pattern:
            raise ValueError(f"condition kind {self.kind!r} requires a pattern")
        return self


# ==========================================================================
# Inputs and outputs
# ==========================================================================


class ParamSpec(BaseModel):
    """A typed input the calling agent supplies per invocation."""

    name: str
    type: ValueType = "string"
    required: bool = True
    description: str = ""
    pattern: str | None = Field(
        default=None, description="Optional regex the value must satisfy before replay starts."
    )
    example: str | None = None
    sensitive: bool = Field(
        default=False,
        description=(
            "If true the value is used but never written to any artifact, log, "
            "or evidence file. Discovery marks credential-shaped fields "
            "automatically; the policy file is the authority."
        ),
    )


class Extraction(BaseModel):
    """Where an output value comes from on the final surface state.

    ``attribute`` is currently always treated as ``"value"`` by the replay
    engine regardless of what's declared here -- the surface's read action
    (``grip/surface/web.py``) computes one unified concept, "the content
    this node exists to convey" (see its module docstring), not three
    separate text/value/name readings. ``"text"``/``"name"`` are reserved
    for a surface that can genuinely distinguish them; declaring one today
    is accepted but has no effect. Defaults to ``"value"`` -- the thing
    that's actually implemented -- rather than ``"text"``, so an artifact
    that omits this field isn't told one thing and given another.
    """

    locator: AXLocator
    attribute: Literal["text", "value", "name"] = "value"
    capture_pattern: str | None = Field(
        default=None,
        description="Optional regex; if it has a capture group, group 1 becomes the value.",
    )


class OutputSpec(BaseModel):
    """A typed value the capability returns to its caller."""

    name: str
    type: ValueType = "string"
    description: str = ""
    source: Extraction
    required: bool = Field(
        default=True,
        description="If true, a missing value on replay is a hard failure rather than a null.",
    )


# ==========================================================================
# Outcomes: the three-way result contract
# ==========================================================================


class BusinessOutcome(BaseModel):
    """A legitimate, expected, non-success answer.

    'No such member' is the canonical example. The caller needs to know it
    happened; it is not an error and must not be reported as one. Declaring
    these on the artifact is what lets replay distinguish them from failures
    without an LLM re-reading the screen.
    """

    code: str = Field(description="Stable machine code, e.g. MEMBER_NOT_FOUND.")
    description: str
    detect: Condition
    outputs: dict[str, str] = Field(
        default_factory=dict,
        description="Static values returned alongside this outcome, if any.",
    )


class RecoveryRule(BaseModel):
    """A known, bounded, deterministic response to a transient condition.

    Recovery is intentionally *not* open-ended. Each rule is a declared
    detect-then-do pair with an attempt cap. There is no model in this loop;
    an unrecognised condition escalates rather than improvising.
    """

    code: str = Field(description="Stable machine code, e.g. DISMISS_MOTD_INTERSTITIAL.")
    description: str
    detect: Condition
    action: ActionType
    target: AXLocator | None = None
    value: str | None = None
    max_attempts: int = 2

    @model_validator(mode="after")
    def _check_target(self) -> "RecoveryRule":
        if self.action in {"click", "type", "select"} and self.target is None:
            raise ValueError(f"recovery action {self.action!r} requires a target")
        return self


class EscalationRule(BaseModel):
    """A condition that must go to a human rather than being handled."""

    code: str
    description: str
    detect: Condition
    guidance: str = Field(
        default="", description="What the human operator is being asked to do."
    )


# ==========================================================================
# Steps
# ==========================================================================


class ValueSource(BaseModel):
    """Where a step's input value comes from."""

    kind: Literal["literal", "param", "extracted"]
    # literal
    value: str | None = None
    # param
    param: str | None = None
    # extracted — value read by an earlier step in this run
    from_output: str | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> "ValueSource":
        required = {"literal": "value", "param": "param", "extracted": "from_output"}[self.kind]
        if getattr(self, required) is None:
            raise ValueError(f"value source of kind {self.kind!r} requires {required!r}")
        return self

    def describe(self) -> str:
        if self.kind == "param":
            return f"${{{self.param}}}"
        if self.kind == "extracted":
            return f"<-{self.from_output}"
        return repr(self.value)


class Step(BaseModel):
    """One ordered action in the recorded flow."""

    index: int
    action: ActionType
    target: AXLocator | None = None
    value: ValueSource | None = None

    route_template: str | None = Field(
        default=None,
        description=(
            "For navigate steps: the canonicalised route with parameters as "
            "placeholders, e.g. '/member/:member_id'. Storing the template "
            "rather than the concrete URL is what makes the artifact reusable "
            "across tenants and across different record IDs."
        ),
    )

    checkpoint: Condition | None = Field(
        default=None,
        description=(
            "Post-condition asserted after the action. Without this, replay is "
            "assuming the click worked rather than verifying it."
        ),
    )
    settle_ms: int = Field(
        default=0, description="Extra quiet period after the action, if the app needs it."
    )
    risk: RiskClass = "safe"
    captures: str | None = Field(
        default=None,
        description="Name under which a 'read' step's value is stored for later steps.",
    )
    note: str = ""

    @model_validator(mode="after")
    def _check_shape(self) -> "Step":
        if self.action in {"click", "select"} and self.target is None:
            raise ValueError(f"step {self.index}: action {self.action!r} requires a target")
        if self.action == "type":
            if self.target is None:
                raise ValueError(f"step {self.index}: 'type' requires a target")
            if self.value is None:
                raise ValueError(f"step {self.index}: 'type' requires a value source")
        if self.action == "navigate" and not (self.route_template or self.value):
            raise ValueError(f"step {self.index}: 'navigate' requires a route_template or value")
        if self.action == "read":
            if self.target is None:
                raise ValueError(f"step {self.index}: 'read' requires a target")
            if not self.captures:
                raise ValueError(f"step {self.index}: 'read' requires a captures name")
        return self


# ==========================================================================
# Surface binding and cross-tenant variance
# ==========================================================================


class SurfaceBinding(BaseModel):
    """What kind of surface this capability drives, and where it starts.

    ``product`` is the load-bearing field for multi-tenant reuse: hundreds of
    institutions run the same vendor product, so an artifact is keyed to the
    *product and version*, and a tenant is a variant of it — not a separate
    recording.
    """

    kind: Literal["web", "desktop"] = "web"
    entry: str = Field(description="Entry URL (web) or application launch identifier (desktop).")
    product: str | None = Field(
        default=None, description="Vendor product identifier this flow was recorded against."
    )
    product_version: str | None = None
    recorded_tenant: str | None = Field(
        default=None, description="Tenant the recording was made against."
    )


class TenantOverride(BaseModel):
    """A narrow, reviewable patch for one tenant's variant of the same product.

    The design intent is that overrides stay small. A tenant needing many
    overrides is a signal that it is running a genuinely different version,
    which should be caught and surfaced rather than papered over.
    """

    tenant: str
    entry: str | None = None
    step_targets: dict[int, AXLocator] = Field(
        default_factory=dict, description="Step index -> replacement locator."
    )
    param_defaults: dict[str, str] = Field(default_factory=dict)
    disabled_steps: list[int] = Field(default_factory=list)
    note: str = ""


# ==========================================================================
# Provenance
# ==========================================================================


class Provenance(BaseModel):
    """How this artifact came to exist. Required for review and for audit."""

    discovered_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    discovery_run_id: str = ""
    model: str = ""
    model_base_url: str = ""
    steps_explored: int = 0
    human_interventions: int = 0
    policy_version: str = "1"
    content_hash: str = ""


class ApprovalState(BaseModel):
    """Gate for unattended execution.

    A freshly discovered artifact is a draft. Promoting it to approved is a
    human act, because the first successful run is evidence that the flow
    works once, not that it is safe to run unattended against production.
    """

    state: Literal["draft", "approved", "deprecated"] = "draft"
    approved_by: str | None = None
    approved_at: str | None = None
    note: str = ""


# ==========================================================================
# The artifact
# ==========================================================================


class CapabilityArtifact(BaseModel):
    """A versioned, reviewable, agent-invocable capability."""

    schema_version: str = SCHEMA_VERSION
    capability_id: str = Field(description="Stable slug, e.g. 'lookup_member_savings_balance'.")
    version: int = 1
    title: str
    description: str = Field(
        description="What a calling agent needs to know to decide whether to invoke this."
    )
    goal: str = Field(description="The original natural-language goal that produced this flow.")

    surface: SurfaceBinding
    params: list[ParamSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    steps: list[Step]

    success: Condition = Field(description="Overall success condition for the flow.")
    business_outcomes: list[BusinessOutcome] = Field(default_factory=list)
    recoveries: list[RecoveryRule] = Field(default_factory=list)
    escalations: list[EscalationRule] = Field(default_factory=list)

    tenant_overrides: list[TenantOverride] = Field(default_factory=list)
    approval: ApprovalState = Field(default_factory=ApprovalState)
    provenance: Provenance = Field(default_factory=Provenance)

    # -- helpers ----------------------------------------------------------

    def param(self, name: str) -> ParamSpec | None:
        return next((p for p in self.params if p.name == name), None)

    def sensitive_params(self) -> set[str]:
        return {p.name for p in self.params if p.sensitive}

    def for_tenant(self, tenant: str | None) -> "CapabilityArtifact":
        """Return a copy specialised for one tenant.

        Reuse is the default and specialisation is explicit: with no matching
        override the base artifact is returned unchanged, which is the
        behaviour we want for the majority of tenants running a stock install.
        """
        if not tenant:
            return self
        override = next((o for o in self.tenant_overrides if o.tenant == tenant), None)
        if override is None:
            return self

        clone = self.model_copy(deep=True)
        if override.entry:
            clone.surface.entry = override.entry
        clone.surface.recorded_tenant = tenant
        kept: list[Step] = []
        for step in clone.steps:
            if step.index in override.disabled_steps:
                continue
            if step.index in override.step_targets:
                original_target = step.target
                new_target = override.step_targets[step.index]
                step.target = new_target
                # A step's own checkpoint commonly re-asserts against the
                # same control it just acted on (e.g. "the field now holds
                # what we typed"). If that checkpoint's locator is the same
                # control as the target we just overrode, it needs the same
                # patch -- otherwise the override silently only half-applies:
                # the action succeeds against the tenant's control, and the
                # very next line fails asserting against the original one.
                if (
                    step.checkpoint is not None
                    and step.checkpoint.locator is not None
                    and original_target is not None
                    and step.checkpoint.locator.identity_key() == original_target.identity_key()
                ):
                    step.checkpoint.locator = new_target
            kept.append(step)
        clone.steps = kept
        return clone

    def compute_hash(self) -> str:
        """Content hash over the executable parts only.

        Provenance and approval are excluded so that approving an artifact or
        re-recording it at a later date does not change its identity.
        """
        payload = self.model_dump(
            mode="json", exclude={"provenance", "approval"}
        )
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def seal(self) -> "CapabilityArtifact":
        self.provenance.content_hash = self.compute_hash()
        return self

    def to_tool_schema(self) -> dict[str, Any]:
        """Render as an OpenAI-style function schema.

        This is the whole point of typing the artifact: a calling agent
        discovers capabilities as callable tools without knowing anything
        about browsers, accessibility trees, or the flow itself.
        """
        type_map = {
            "string": "string",
            "integer": "integer",
            "number": "number",
            "boolean": "boolean",
            "date": "string",
            "money": "string",
        }
        props: dict[str, Any] = {}
        for p in self.params:
            entry: dict[str, Any] = {"type": type_map[p.type], "description": p.description}
            if p.pattern:
                entry["pattern"] = p.pattern
            props[p.name] = entry
        return {
            "type": "function",
            "function": {
                "name": self.capability_id,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": [p.name for p in self.params if p.required],
                },
            },
        }


# ==========================================================================
# Replay result contract
# ==========================================================================


class StepTrace(BaseModel):
    """What happened on one replayed step. The debugging unit."""

    index: int
    action: ActionType
    target: str | None = None
    value: str | None = Field(default=None, description="Redacted if the source was sensitive.")
    status: Literal["ok", "recovered", "failed", "skipped"]
    expected: str | None = None
    observed: str | None = None
    recoveries_applied: list[str] = Field(default_factory=list)
    duration_ms: int = 0
    detail: str = ""
    resolved: str | None = Field(
        default=None,
        description=(
            "How the step's locator was actually matched -- 'primary' or which "
            "fallback strategy. Null for steps with no locator (navigate, wait). "
            "A capability that only ever resolves via fallbacks is drifting even "
            "while it still technically replays; grip/reliability.py is what "
            "finally reads this back rather than letting it go to waste."
        ),
    )


class ReplayResult(BaseModel):
    """The three-way contract a caller receives.

    ``success``  — the flow completed and the success condition held.
    ``business`` — a declared, expected non-success answer. Not an error.
    ``failure``  — everything else, with enough detail to debug.
    ``escalated``— the run stopped and is awaiting or has had human action.
    """

    status: Literal["success", "business", "failure", "escalated"]
    capability_id: str
    capability_version: int
    content_hash: str = ""
    tenant: str | None = None
    run_id: str = ""

    outputs: dict[str, Any] = Field(default_factory=dict)

    outcome_code: str | None = Field(
        default=None, description="Set when status is 'business': e.g. MEMBER_NOT_FOUND."
    )
    outcome_description: str | None = None

    failure_kind: (
        Literal[
            "locator_not_found",
            "checkpoint_failed",
            "output_missing",
            "guardrail_blocked",
            "timeout",
            "unrecognised_state",
            "surface_error",
        ]
        | None
    ) = None
    failed_step: int | None = None
    expected: str | None = None
    observed: str | None = None
    message: str = ""

    escalation_id: str | None = None
    steps: list[StepTrace] = Field(default_factory=list)
    duration_ms: int = 0
    evidence_dir: str | None = None

    @property
    def ok(self) -> bool:
        """True when the caller got a usable answer — success or a business outcome.

        Deliberately not the same as ``status == 'success'``. A caller asking
        'did this work' and a caller asking 'did this succeed' are asking
        different questions, and the distinction is the point.
        """
        return self.status in {"success", "business"}
