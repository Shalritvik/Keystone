"""Scores how much evidence exists that an artifact is safe to approve.

``ApprovalState``'s own docstring (keystone/schemas.py) makes a specific claim:
"the first successful run is evidence that the flow works once, not that it
is safe to run unattended against production." Nothing before this module
actually measured the gap between those two things -- this closes it, using
data the system was already producing and throwing away.

Two signals, neither of which required new instrumentation:

* **Empirical** -- replay the artifact N times and see what fraction come
  back ``.ok`` (success or a declared business outcome; both are usable
  answers). A capability that only sometimes completes is exactly what an
  approver needs to know before promoting draft -> approved.
* **Structural** -- ``Surface.resolve()`` already reports whether it matched
  a locator via its primary strategy or a fallback (``StepTrace.resolved``,
  wired through in this same phase). A capability that only limps along on
  fallbacks is drifting from the surface it was recorded against, even on
  runs that technically still pass -- which the pass-rate number alone
  would never reveal.

This is deliberately not "scaling infrastructure": it is one more thing
``ReplayEngine`` (already built) is called to do, N times, in a loop, using
a fresh browser context each time per the multi-tenant isolation rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from keystone.guardrails import GuardedSurface
from keystone.replay.engine import ReplayEngine
from keystone.schemas import CapabilityArtifact

SurfaceFactory = Callable[[], Awaitable[GuardedSurface]]


@dataclass
class ReliabilityReport:
    runs: int = 0
    passes: int = 0
    failures: int = 0
    primary_resolutions: int = 0
    fallback_resolutions: int = 0
    statuses: list[str] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return self.passes / self.runs if self.runs else 0.0

    @property
    def primary_resolution_rate(self) -> float:
        total = self.primary_resolutions + self.fallback_resolutions
        return self.primary_resolutions / total if total else 1.0

    @property
    def is_healthy(self) -> bool:
        """The bar for "approve without an explicit override": every run

        completed with a usable answer, and every locator that resolved did
        so via its primary match -- no fallback quietly papering over
        drift. This is a recommendation, not a gate the tool enforces on
        its own: an approver can always override it (see keystone/cli.py's
        ``approve --force``), but they do so having been told, not having
        guessed.
        """
        return self.runs > 0 and self.pass_rate == 1.0 and self.primary_resolution_rate == 1.0

    def summary(self) -> str:
        return (
            f"{self.runs} run(s): pass_rate={self.pass_rate:.0%} "
            f"primary_resolution_rate={self.primary_resolution_rate:.0%} "
            f"statuses={self.statuses}"
        )


async def assess(
    surface_factory: SurfaceFactory,
    evidence_dir: Path | str,
    artifact: CapabilityArtifact,
    params: dict[str, str],
    *,
    tenant: str | None = None,
    n_runs: int = 3,
) -> ReliabilityReport:
    report = ReliabilityReport()

    for _ in range(n_runs):
        surface = await surface_factory()
        try:
            engine = ReplayEngine(surface, evidence_dir)
            result = await engine.run(artifact, params, tenant=tenant)
        finally:
            await surface.close()

        report.runs += 1
        report.statuses.append(result.status)
        report.run_ids.append(result.run_id)
        if result.ok:
            report.passes += 1
        else:
            report.failures += 1

        for trace in result.steps:
            if trace.resolved == "primary":
                report.primary_resolutions += 1
            elif trace.resolved is not None:
                report.fallback_resolutions += 1

    return report
