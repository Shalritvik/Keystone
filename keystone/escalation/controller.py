"""The pause/handoff/resume seam.

The scope note in the brief is explicit: a full real-time co-browsing
console is out of scope; what matters is that the handoff mechanism and the
control-transfer model are real. Concretely, "real" means one thing here --
the human operates the *same* browser the automation was driving, not a
screenshot relay or a fresh session. That is why ``EscalationController``
holds a reference to the same ``GuardedSurface`` (and, underneath it, the
same live Playwright/Chromium session) the run was already using, and why
running headed (``KEYSTONE_HEADLESS=0``) is what makes this real rather than
theoretical: the Chromium window is a real OS window a person can click into
directly the instant automation stops sending it commands, and it is still
sitting on exactly the state the run left it in.

Who is "in control" is tracked explicitly (``self._control``) rather than
inferred, because the brief calls this out by name: "there must be a way to
know who is (or should be) in control." Automation never acts while a human
has it, and nothing here lets it -- ``raise_intervention`` blocks the caller
until ``resume()`` is called from outside (the console, or a human driving
it directly), which is the actual pause: the coroutine that was about to act
simply does not proceed.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from keystone.evidence import EscalationRaised, EscalationResumed, EvidenceWriter
from keystone.guardrails import GuardedSurface

Control = Literal["automation", "human"]


@dataclass
class InterventionRequest:
    """Everything the brief asks an intervention request to carry: which

    capability/goal, the current step, the current state (screenshot +
    a rendered summary), and why it stopped.
    """

    request_id: str
    run_id: str
    capability_id: str | None
    goal: str | None
    step_index: int | None
    reason: str
    screenshot_path: str | None
    surface_summary: str
    created_at: str
    status: Literal["pending", "resumed"] = "pending"
    note: str = ""


class EscalationController:
    """One instance per run. Owns the pause/resume seam for that run's

    surface, not a global registry -- a controller escalating run A has no
    way to affect run B's session, which is the same isolation boundary
    "one browser context per run" already enforces elsewhere.
    """

    def __init__(self, surface: GuardedSurface, writer: EvidenceWriter) -> None:
        self._surface = surface
        self._writer = writer
        self._control: Control = "automation"
        self._pending: InterventionRequest | None = None
        self._resume_event = asyncio.Event()

    @property
    def control(self) -> Control:
        return self._control

    @property
    def pending(self) -> InterventionRequest | None:
        return self._pending

    async def raise_intervention(
        self,
        *,
        run_id: str,
        capability_id: str | None,
        goal: str | None,
        step_index: int | None,
        reason: str,
        screenshot_dir: Path,
    ) -> InterventionRequest:
        """Pauses automation and blocks until a human calls ``resume()``.

        The caller (the replay engine or the discovery loop) awaits this
        directly -- there is no separate "check if paused" poll loop. The
        coroutine that would have acted next simply does not resume running
        until a human, through the console, releases it.
        """
        request_id = uuid.uuid4().hex[:12]
        observation = await self._surface.observe()

        screenshot_path: str | None = None
        shot = screenshot_dir / f"escalation_{request_id}.png"
        if await self._surface.screenshot(str(shot)):
            screenshot_path = str(shot)

        request = InterventionRequest(
            request_id=request_id,
            run_id=run_id,
            capability_id=capability_id,
            goal=goal,
            step_index=step_index,
            reason=reason,
            screenshot_path=screenshot_path,
            surface_summary=observation.render(max_nodes=40),
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self._pending = request
        self._control = "human"
        self._resume_event.clear()

        self._writer.write(
            EscalationRaised(
                run_id=run_id, request_id=request_id, capability_id=capability_id,
                goal=goal, step_index=step_index, reason=reason, screenshot=screenshot_path,
            )
        )

        await self._resume_event.wait()
        return request

    def resume(self, note: str = "") -> InterventionRequest:
        """Hands control back to automation. Called by the console (or,

        in a headed demo, by a human acting directly on the session and
        then telling the console they're done) -- never by automation
        itself, which is what keeps "who is in control" unambiguous.
        """
        if self._pending is None:
            raise RuntimeError("resume() called with no pending intervention")
        self._pending.status = "resumed"
        self._pending.note = note
        self._control = "automation"
        self._writer.write(EscalationResumed(run_id=self._pending.run_id, request_id=self._pending.request_id, note=note))
        resolved = self._pending
        self._pending = None
        self._resume_event.set()
        return resolved
