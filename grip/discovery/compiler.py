"""Turns a recorded discovery trace into a ``CapabilityArtifact``.

Pure transformation, no I/O and no live surface calls: every ``DiscoveredStep``
already carries a durable ``AXLocator`` (converted from the live ``AXNode`` at
the moment it was acted on, while the ref was still valid -- refs are
ephemeral, locators are durable, and the boundary between the two is exactly
the agent-loop/compiler boundary). ``Policy.classify`` is pure pattern
matching over already-loaded config, not I/O, so it's fine to call here too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from grip.config import Policy
from grip.schemas import (
    AXLocator,
    CapabilityArtifact,
    Condition,
    Extraction,
    OutputSpec,
    ParamSpec,
    Provenance,
    Step,
    SurfaceBinding,
    ValueSource,
)


@dataclass
class DiscoveredStep:
    """One successful action from the discovery loop, already carrying a

    durable locator rather than the ephemeral ref it was addressed by live.
    """

    index: int
    action: str
    locator: AXLocator | None = None
    value: str | None = None
    param_name: str | None = None
    output_name: str | None = None
    read_value: str | None = None
    reason: str = ""
    sensitive: bool = False


def compile_artifact(
    *,
    capability_id: str,
    goal: str,
    entry_url: str,
    tenant: str | None,
    trace: list[DiscoveredStep],
    policy: Policy,
    discovery_run_id: str,
    model: str,
    model_base_url: str,
) -> CapabilityArtifact:
    params: dict[str, ParamSpec] = {}
    steps: list[Step] = []

    for i, ds in enumerate(trace):
        value_source = _value_source(ds, params)
        checkpoint = _synthesize_checkpoint(ds, trace, i)
        risk = policy.classify(ds.action, ds.locator.name if ds.locator else None)

        steps.append(
            Step(
                index=ds.index,
                action=ds.action,
                target=ds.locator,
                value=value_source,
                checkpoint=checkpoint,
                captures=ds.output_name if ds.action == "read" else None,
                risk=risk,
                note=ds.reason,
            )
        )

    read_steps = [ds for ds in trace if ds.action == "read" and ds.output_name]
    outputs = [
        OutputSpec(
            name=ds.output_name,  # type: ignore[arg-type]
            type="string",
            description=f"Extracted by the discovery run: {ds.reason}" if ds.reason else "",
            required=True,
            source=Extraction(locator=ds.locator, attribute="value"),  # type: ignore[arg-type]
        )
        for ds in read_steps
    ]

    return CapabilityArtifact(
        capability_id=capability_id,
        version=1,
        title=capability_id.replace("_", " ").title(),
        description=f"Auto-discovered capability for the goal: {goal!r}. Draft -- human review required before approval.",
        goal=goal,
        surface=SurfaceBinding(kind="web", entry=entry_url, recorded_tenant=tenant),
        params=list(params.values()),
        outputs=outputs,
        steps=steps,
        success=_success_condition(read_steps),
        provenance=Provenance(
            discovery_run_id=discovery_run_id,
            model=model,
            model_base_url=model_base_url,
            steps_explored=len(trace),
            policy_version=policy.version,
        ),
    )


def _value_source(ds: DiscoveredStep, params: dict[str, ParamSpec]) -> ValueSource | None:
    if ds.action not in ("type", "select"):
        return None

    if ds.sensitive:
        # Never a literal, regardless of what the model tagged -- design
        # rule 9. A sensitive field always becomes a param with no stored
        # example, even if the model didn't think to name it as one.
        name = ds.param_name or f"field_{ds.index}"
        params.setdefault(name, ParamSpec(name=name, required=True, sensitive=True))
        return ValueSource(kind="param", param=name)

    if ds.param_name:
        params.setdefault(
            ds.param_name,
            ParamSpec(
                name=ds.param_name,
                required=True,
                example=ds.value,
                description=f"Value for {ds.locator.describe()}" if ds.locator else ds.param_name,
            ),
        )
        return ValueSource(kind="param", param=ds.param_name)

    return ValueSource(kind="literal", value=ds.value)


def _synthesize_checkpoint(ds: DiscoveredStep, trace: list[DiscoveredStep], i: int) -> Condition | None:
    if ds.action in ("type", "select") and ds.locator is not None:
        pattern = f"^${{{ds.param_name}}}$" if ds.param_name else f"^{re.escape(ds.value or '')}$"
        return Condition(
            kind="ax_value_matches",
            locator=ds.locator,
            pattern=pattern,
            description="the field visibly holds the value just set",
        )

    if ds.action == "click" and i + 1 < len(trace):
        # The click's own effect is best asserted by what the *next* step
        # needs to already be true -- e.g. "click Search" is checkpointed by
        # "the value the next read step wants is now present," not by the
        # URL, since a business outcome sharing the same route would
        # otherwise be indistinguishable from success at this checkpoint.
        nxt = trace[i + 1]
        if nxt.action in ("read", "type", "select") and nxt.locator is not None:
            return Condition(
                kind="ax_present",
                locator=nxt.locator,
                description="the control the next step needs is now present",
            )

    return None


def _success_condition(read_steps: list[DiscoveredStep]) -> Condition:
    if not read_steps:
        return Condition(
            kind="url_matches", pattern=".*",
            description="no extraction occurred during discovery; no stronger condition to synthesise",
        )
    last = read_steps[-1]
    return Condition(
        kind="ax_present",
        locator=last.locator,
        description=f"the {last.output_name} value is present",
    )
