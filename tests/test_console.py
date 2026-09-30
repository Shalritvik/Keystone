"""Tests the WebSocket console as an actual client/server exchange -- the

protocol genuinely works, even though the "operator UI" on top of it is
deliberately just this raw JSON-lines exchange (see keystone/escalation/console.py).
"""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from keystone.escalation.console import serve_console
from keystone.escalation.controller import EscalationController
from keystone.evidence import EvidenceWriter
from keystone.surface.base import ActionOutcome, ActionRequest, AXNode, Observation, Surface


class FakeSurface(Surface):
    kind = "fake"

    async def observe(self) -> Observation:
        return Observation(url="http://x/review", title="t", nodes=[AXNode(ref="n0", role="status", name="ok")])

    async def act(self, request: ActionRequest) -> ActionOutcome:
        return ActionOutcome(ok=True)

    async def resolve(self, locator):
        return "n0", "primary"

    async def screenshot(self, path: str) -> bool:
        return False

    async def current_url(self) -> str:
        return "http://x/review"

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_console_reports_idle_then_pending_then_accepts_resume(tmp_path):
    controller = EscalationController(FakeSurface(), EvidenceWriter(tmp_path))
    server = await serve_console(controller, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as client:
            first = json.loads(await client.recv())
            assert first == {"type": "idle", "control": "automation"}

            escalate_task = asyncio.create_task(
                controller.raise_intervention(
                    run_id="r1", capability_id="cap", goal=None, step_index=1,
                    reason="risky", screenshot_dir=tmp_path,
                )
            )
            await asyncio.sleep(0.1)

            await client.send(json.dumps({"action": "status"}))
            status = json.loads(await client.recv())
            assert status["type"] == "pending"
            assert status["request"]["reason"] == "risky"

            await client.send(json.dumps({"action": "resume", "note": "ok, go ahead"}))
            resumed = json.loads(await client.recv())
            assert resumed["type"] == "resumed"
            assert resumed["request"]["note"] == "ok, go ahead"

            request = await escalate_task
            assert request.status == "resumed"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_console_reports_pending_immediately_on_connect_if_already_escalated(tmp_path):
    controller = EscalationController(FakeSurface(), EvidenceWriter(tmp_path))
    escalate_task = asyncio.create_task(
        controller.raise_intervention(
            run_id="r1", capability_id="cap", goal="look something up", step_index=0,
            reason="stuck", screenshot_dir=tmp_path,
        )
    )
    await asyncio.sleep(0.1)

    server = await serve_console(controller, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as client:
            first = json.loads(await client.recv())
            assert first["type"] == "pending"
            assert first["request"]["goal"] == "look something up"

            await client.send(json.dumps({"action": "resume"}))
            await client.recv()
    finally:
        await escalate_task
        server.close()
        await server.wait_closed()
