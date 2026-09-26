"""Deterministic replay. No LLM anywhere in this file -- design rule 1.

That is enforced by absence, not by a runtime check: nothing here imports
``grip.llm``, and the only surface this engine is ever handed is a
``GuardedSurface`` (grip/guardrails.py), never a raw one. Replay resolves
stored locators, asserts checkpoints, and returns a typed
:class:`~grip.schemas.ReplayResult`. An unrecognised state is an escalation
candidate or a hard failure, never a reason to call a model.

The per-step control flow, in order, mirrors CLAUDE.md's contract exactly:

1. Resolve the step's locator (if any) against the live surface.
2. Pass the guardrail (structurally: the surface it acts through is a
   ``GuardedSurface``, so this is not optional).
3. Act.
4. If the action succeeded, opportunistically clear any recovery condition
   that is now blocking the page (this is what lets the message-of-the-day
   interstitial get dismissed the instant it appears, including on the very
   first page load, before it can interfere with the next action).
5. Assert the step's checkpoint, if it has one.
6. If the action failed *or* the checkpoint failed: check declared business
   outcomes first (a legitimate answer, not an error), then try a recovery
   rule reactively and retry the step if one applied, then check escalation
   rules, and only then report a hard failure with the failing step index,
   what was expected, what was observed, and a screenshot.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from grip.conditions import evaluate as evaluate_condition
from grip.conditions import substitute as _substitute
from grip.evidence import REDACTED, EvidenceWriter, RunFinished, RunStarted, StepFinished, StepStarted
from grip.guardrails import GuardedSurface
from grip.schemas import (
    BusinessOutcome,
    CapabilityArtifact,
    EscalationRule,
    ReplayResult,
    Step,
    StepTrace,
    ValueSource,
)
from grip.surface.base import ActionRequest


class ReplayError(RuntimeError):
    """Raised for a caller/invocation mistake -- bad params, unknown tenant.

    Distinct from a ``ReplayResult`` status: those three describe legitimate
    outcomes of a correctly-invoked run against the live surface. This is
    "you called the API wrong," which is not a fact about the surface at all.
    """


# ==========================================================================
# Internal control-flow signals
#
# Each step can end in exactly one of four ways once it stops looping:
# clean success (returned normally), a business outcome, an escalation, or a
# hard failure. The latter three need to unwind out of a step deep inside
# the loop straight to run()'s result-building code, which is what
# exceptions are for here -- they never cross this module's boundary.
# ==========================================================================


class _BusinessSignal(Exception):
    def __init__(self, outcome: BusinessOutcome) -> None:
        self.outcome = outcome


class _EscalationSignal(Exception):
    def __init__(self, rule: EscalationRule) -> None:
        self.rule = rule


class _FailureSignal(Exception):
    def __init__(self, *, step_index: int, failure_kind: str, expected: str | None,
                 observed: str | None, message: str) -> None:
        self.step_index = step_index
        self.failure_kind = failure_kind
        self.expected = expected
        self.observed = observed
        self.message = message


_ROUTE_TOKEN = re.compile(r":([a-zA-Z_][a-zA-Z0-9_]*)")


def _resolve_route(template: str, params: dict[str, str]) -> str:
    return _ROUTE_TOKEN.sub(lambda m: params.get(m.group(1), m.group(0)), template)


@dataclass
class _RunContext:
    artifact: CapabilityArtifact
    params: dict[str, str]
    tenant: str | None
    run_id: str
    writer: EvidenceWriter
    captures: dict[str, str] = field(default_factory=dict)
    recovery_counts: dict[str, int] = field(default_factory=dict)


class ReplayEngine:
    """Executes one ``CapabilityArtifact`` invocation against a live surface."""

    def __init__(self, surface: GuardedSurface, evidence_dir: Path | str) -> None:
        self._surface = surface
        self._evidence_dir = Path(evidence_dir)

    # -- public entry point --------------------------------------------

    async def run(
        self,
        artifact: CapabilityArtifact,
        params: dict[str, str],
        *,
        tenant: str | None = None,
        run_id: str | None = None,
    ) -> ReplayResult:
        artifact = artifact.for_tenant(tenant)
        self._validate_params(artifact, params)

        run_id = run_id or f"replay-{artifact.capability_id}-{uuid.uuid4().hex[:8]}"
        writer = EvidenceWriter(self._evidence_dir / run_id)
        ctx = _RunContext(artifact=artifact, params=params, tenant=tenant, run_id=run_id, writer=writer)
        start = time.monotonic()

        writer.write(
            RunStarted(
                run_id=run_id,
                capability_id=artifact.capability_id,
                tenant=tenant,
                entry_url=artifact.surface.entry,
                params=dict(params),
                sensitive_params=frozenset(artifact.sensitive_params()),
            )
        )

        traces: list[StepTrace] = []
        try:
            entry_step = Step(index=-1, action="navigate", route_template=artifact.surface.entry)
            traces.append(await self._run_step(ctx, entry_step, url_override=artifact.surface.entry))

            for step in artifact.steps:
                traces.append(await self._run_step(ctx, step))

            ok, observed = await evaluate_condition(self._surface, artifact.success, ctx.params)
            if not ok:
                last_index = artifact.steps[-1].index if artifact.steps else -1
                raise _FailureSignal(
                    step_index=last_index,
                    failure_kind="checkpoint_failed",
                    expected=artifact.success.description or "success condition",
                    observed=observed,
                    message="all steps completed but the declared success condition did not hold",
                )

            outputs = await self._extract_outputs(ctx)
            result = self._finish(ctx, traces, start, status="success", outputs=outputs)

        except _BusinessSignal as sig:
            result = self._finish(
                ctx, traces, start, status="business",
                outputs=dict(sig.outcome.outputs),
                outcome_code=sig.outcome.code, outcome_description=sig.outcome.description,
            )
        except _EscalationSignal as sig:
            result = self._finish(ctx, traces, start, status="escalated", escalation_id=sig.rule.code)
        except _FailureSignal as sig:
            result = self._finish(
                ctx, traces, start, status="failure",
                failure_kind=sig.failure_kind, failed_step=sig.step_index,
                expected=sig.expected, observed=sig.observed, message=sig.message,
            )
        finally:
            writer.close()

        return result

    def _finish(self, ctx: _RunContext, traces: list[StepTrace], start: float, *, status: str, **fields: Any) -> ReplayResult:
        duration_ms = int((time.monotonic() - start) * 1000)
        ctx.writer.write(RunFinished(run_id=ctx.run_id, status=status, duration_ms=duration_ms))
        result = ReplayResult(
            status=status,
            capability_id=ctx.artifact.capability_id,
            capability_version=ctx.artifact.version,
            content_hash=ctx.artifact.provenance.content_hash,
            tenant=ctx.tenant,
            run_id=ctx.run_id,
            steps=traces,
            duration_ms=duration_ms,
            evidence_dir=str(ctx.writer.run_dir),
            **fields,
        )
        ctx.writer.write_result(result.model_dump(mode="json"))
        return result

    # -- one step, including its recovery/retry loop ---------------------

    async def _run_step(self, ctx: _RunContext, step: Step, *, url_override: str | None = None) -> StepTrace:
        recoveries_applied: list[str] = []
        step_start = time.monotonic()

        while True:
            target_desc = step.target.describe() if step.target else (url_override or step.route_template)
            value_str: str | None = None
            sensitive = False
            if step.value is not None:
                value_str = self._resolve_value(step.value, ctx)
                if step.value.kind == "param":
                    spec = ctx.artifact.param(step.value.param)
                    sensitive = bool(spec and spec.sensitive)

            ctx.writer.write(
                StepStarted(
                    run_id=ctx.run_id, step_index=step.index, action=step.action,
                    target=target_desc, value=value_str, sensitive=sensitive,
                )
            )

            act_ok, act_detail, act_kind, read_value = await self._attempt(
                ctx, step, value_str, sensitive, url_override
            )

            if act_ok:
                await self._recover_until_clear(ctx, recoveries_applied)

            checkpoint_ok = True
            observed: str | None = None
            expected: str | None = None
            if act_ok and step.checkpoint is not None:
                checkpoint_ok, observed = await evaluate_condition(self._surface, step.checkpoint, ctx.params)
                expected = step.checkpoint.description or step.checkpoint.pattern

            if act_ok and checkpoint_ok:
                if step.action == "read" and step.captures:
                    ctx.captures[step.captures] = read_value or ""
                duration_ms = int((time.monotonic() - step_start) * 1000)
                status = "recovered" if recoveries_applied else "ok"
                ctx.writer.write(
                    StepFinished(
                        run_id=ctx.run_id, step_index=step.index, status=status,
                        observed=observed, detail=act_detail, duration_ms=duration_ms,
                        recoveries_applied=list(recoveries_applied), sensitive=sensitive,
                    )
                )
                return StepTrace(
                    index=step.index, action=step.action, target=target_desc,
                    value=(REDACTED if sensitive else value_str), status=status,
                    expected=expected, observed=observed,
                    recoveries_applied=list(recoveries_applied),
                    duration_ms=duration_ms, detail=act_detail,
                )

            # --- step did not complete cleanly: business -> recovery -> escalation -> fail ---
            if expected is None:
                expected = act_detail or "action to succeed"
            if observed is None:
                observed = act_detail

            for outcome in ctx.artifact.business_outcomes:
                holds, _ = await evaluate_condition(self._surface, outcome.detect, ctx.params)
                if holds:
                    self._write_step_failed(ctx, step, step_start, recoveries_applied,
                                             observed=f"business outcome {outcome.code}",
                                             detail=outcome.description)
                    raise _BusinessSignal(outcome)

            if await self._try_recover(ctx, recoveries_applied):
                continue  # cleared the obstacle; retry this step from the top

            for rule in ctx.artifact.escalations:
                holds, _ = await evaluate_condition(self._surface, rule.detect, ctx.params)
                if holds:
                    self._write_step_failed(ctx, step, step_start, recoveries_applied,
                                             observed=f"escalation {rule.code}", detail=rule.description)
                    raise _EscalationSignal(rule)

            screenshot_path = await self._capture_failure_screenshot(ctx, step.index)
            self._write_step_failed(ctx, step, step_start, recoveries_applied,
                                     observed=observed, detail=act_detail, expected=expected,
                                     screenshot=screenshot_path)
            raise _FailureSignal(
                step_index=step.index,
                failure_kind=act_kind or "checkpoint_failed",
                expected=expected, observed=observed, message=act_detail,
            )

    def _write_step_failed(self, ctx: _RunContext, step: Step, step_start: float, recoveries_applied: list[str],
                            *, observed: str | None, detail: str, expected: str | None = None,
                            screenshot: str | None = None) -> None:
        duration_ms = int((time.monotonic() - step_start) * 1000)
        ctx.writer.write(
            StepFinished(
                run_id=ctx.run_id, step_index=step.index, status="failed",
                expected=expected, observed=observed, detail=detail,
                duration_ms=duration_ms, recoveries_applied=list(recoveries_applied),
                screenshot=screenshot,
            )
        )

    # -- acting -----------------------------------------------------------

    async def _attempt(
        self, ctx: _RunContext, step: Step, value_str: str | None, sensitive: bool, url_override: str | None,
    ) -> tuple[bool, str, str | None, str | None]:
        if step.action == "navigate":
            url = url_override or self._navigate_url(ctx, step)
            if url is None:
                return False, "navigate step has no url, value, or route_template", "error", None
            outcome = await self._surface.act(ActionRequest(action="navigate", url=url, settle_ms=step.settle_ms))
            return outcome.ok, outcome.detail, outcome.error_kind, outcome.read_value

        if step.target is None:
            return False, f"step {step.index} ({step.action}) has no target", "error", None

        ref, how = await self._surface.resolve(step.target)
        if ref is None:
            return False, f"locator not found: {how}", "locator_not_found", None

        outcome = await self._surface.act(
            ActionRequest(
                action=step.action, ref=ref, locator=step.target, value=value_str,
                settle_ms=step.settle_ms, sensitive=sensitive,
            )
        )
        return outcome.ok, outcome.detail, outcome.error_kind, outcome.read_value

    def _navigate_url(self, ctx: _RunContext, step: Step) -> str | None:
        if step.value is not None:
            return self._resolve_value(step.value, ctx)
        if step.route_template is None:
            return None
        route = _resolve_route(step.route_template, ctx.params)
        if route.startswith("http://") or route.startswith("https://"):
            return route
        base = urlparse(ctx.artifact.surface.entry)
        return f"{base.scheme}://{base.netloc}{route}"

    def _resolve_value(self, value: ValueSource, ctx: _RunContext) -> str:
        if value.kind == "literal":
            return value.value  # type: ignore[return-value]
        if value.kind == "param":
            return ctx.params[value.param]  # type: ignore[index]
        return ctx.captures[value.from_output]  # type: ignore[index]

    # -- recovery -----------------------------------------------------------

    async def _recover_until_clear(self, ctx: _RunContext, recoveries_applied: list[str], limit: int = 3) -> None:
        """Opportunistic, proactive clearing after a successful action.

        This is what dismisses the message-of-the-day interstitial the
        instant it appears -- including right after the very first
        navigation to the entry URL -- rather than waiting for it to first
        cause some other step's checkpoint to fail.
        """
        for _ in range(limit):
            if not await self._try_recover(ctx, recoveries_applied):
                return

    async def _try_recover(self, ctx: _RunContext, recoveries_applied: list[str]) -> bool:
        for rule in ctx.artifact.recoveries:
            used = ctx.recovery_counts.get(rule.code, 0)
            if used >= rule.max_attempts:
                continue
            holds, _ = await evaluate_condition(self._surface, rule.detect, ctx.params)
            if not holds:
                continue

            value = self._resolve_value(ValueSource(kind="literal", value=rule.value), ctx) if rule.value else None
            if rule.target is not None:
                ref, how = await self._surface.resolve(rule.target)
                if ref is None:
                    continue  # declared but not actually reachable right now; try the next rule
                await self._surface.act(ActionRequest(action=rule.action, ref=ref, locator=rule.target, value=value))
            else:
                await self._surface.act(ActionRequest(action=rule.action, value=value))

            ctx.recovery_counts[rule.code] = used + 1
            recoveries_applied.append(rule.code)
            return True
        return False

    # -- outputs -----------------------------------------------------------

    async def _extract_outputs(self, ctx: _RunContext) -> dict[str, Any]:
        """Re-resolves and re-reads independently, at the very end -- design

        rule 6. Nothing captured mid-flow (``Step.captures``) is trusted as
        the returned value, even if a step captured the same thing already.
        """
        outputs: dict[str, Any] = {}
        for spec in ctx.artifact.outputs:
            ref, how = await self._surface.resolve(spec.source.locator)
            if ref is None:
                if spec.required:
                    raise _FailureSignal(
                        step_index=ctx.artifact.steps[-1].index if ctx.artifact.steps else -1,
                        failure_kind="output_missing",
                        expected=f"output {spec.name!r} locator resolvable",
                        observed=f"not found ({how})",
                        message=f"could not resolve locator for required output {spec.name!r}",
                    )
                continue

            outcome = await self._surface.act(ActionRequest(action="read", ref=ref, locator=spec.source.locator))
            if not outcome.ok:
                if spec.required:
                    raise _FailureSignal(
                        step_index=ctx.artifact.steps[-1].index if ctx.artifact.steps else -1,
                        failure_kind="output_missing",
                        expected=f"output {spec.name!r} readable",
                        observed=outcome.detail,
                        message=f"could not read a value for required output {spec.name!r}",
                    )
                continue

            raw_value = outcome.read_value
            if spec.source.capture_pattern and raw_value is not None:
                match = re.search(spec.source.capture_pattern, raw_value)
                if match:
                    raw_value = match.group(1) if match.groups() else match.group(0)
                elif spec.required:
                    raise _FailureSignal(
                        step_index=ctx.artifact.steps[-1].index if ctx.artifact.steps else -1,
                        failure_kind="output_missing",
                        expected=f"output {spec.name!r} matches capture_pattern",
                        observed=raw_value,
                        message=f"capture_pattern did not match for required output {spec.name!r}",
                    )
                else:
                    raw_value = None

            outputs[spec.name] = self._cast(raw_value, spec.type)
        return outputs

    @staticmethod
    def _cast(value: str | None, value_type: str) -> Any:
        if value is None:
            return None
        if value_type == "integer":
            try:
                return int(value.replace(",", ""))
            except ValueError:
                return value
        if value_type == "number":
            try:
                return float(value.replace(",", ""))
            except ValueError:
                return value
        if value_type == "boolean":
            return value.strip().lower() in {"true", "yes", "1"}
        return value  # string, date, money -- pass through as-displayed

    # -- evidence -----------------------------------------------------------

    async def _capture_failure_screenshot(self, ctx: _RunContext, step_index: int) -> str | None:
        path = ctx.writer.screenshot_dir() / f"step_{step_index}_failure.png"
        ok = await self._surface.screenshot(str(path))
        return str(path) if ok else None

    # -- validation -----------------------------------------------------------

    @staticmethod
    def _validate_params(artifact: CapabilityArtifact, params: dict[str, str]) -> None:
        declared = {p.name for p in artifact.params}
        unknown = set(params) - declared
        if unknown:
            raise ReplayError(f"unknown param(s) for {artifact.capability_id!r}: {sorted(unknown)}")

        for spec in artifact.params:
            value = params.get(spec.name)
            if value is None or value == "":
                if spec.required:
                    raise ReplayError(f"missing required param {spec.name!r}")
                continue
            if spec.pattern and not re.fullmatch(spec.pattern, value):
                raise ReplayError(
                    f"param {spec.name!r} value {value!r} does not match pattern {spec.pattern!r}"
                )
