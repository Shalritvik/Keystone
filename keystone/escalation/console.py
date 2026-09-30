"""A minimal operator console over WebSocket.

Deliberately mocked, per the brief's own scope note: this is a bare text
protocol, not an operator dashboard, and there is no authentication, no
multi-operator queueing, and no UI beyond whatever a human types at a raw
WebSocket client. What is *not* mocked is the mechanism underneath it --
``EscalationController`` already holds the same live surface the run was
using, and every message this console sends or receives is a real read or
write against that controller, not a canned response. A real operator UI
would replace this file's transport and rendering; the control-transfer
model in controller.py would not need to change to support it.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

try:
    import websockets
    from websockets.asyncio.server import Server, ServerConnection
except ImportError:  # pragma: no cover - exercised only if the dep is missing
    websockets = None  # type: ignore[assignment]

from keystone.escalation.controller import EscalationController


async def _handle_client(connection: "ServerConnection", controller: EscalationController) -> None:
    if controller.pending is not None:
        await connection.send(json.dumps({"type": "pending", "request": asdict(controller.pending)}))
    else:
        await connection.send(json.dumps({"type": "idle", "control": controller.control}))

    async for raw in connection:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            await connection.send(json.dumps({"type": "error", "detail": "not valid JSON"}))
            continue

        if message.get("action") == "resume":
            if controller.pending is None:
                await connection.send(json.dumps({"type": "error", "detail": "nothing pending"}))
                continue
            resolved = controller.resume(note=message.get("note", ""))
            await connection.send(json.dumps({"type": "resumed", "request": asdict(resolved)}))
        elif message.get("action") == "status":
            if controller.pending is not None:
                await connection.send(json.dumps({"type": "pending", "request": asdict(controller.pending)}))
            else:
                await connection.send(json.dumps({"type": "idle", "control": controller.control}))
        else:
            await connection.send(json.dumps({"type": "error", "detail": f"unknown action {message.get('action')!r}"}))


async def serve_console(controller: EscalationController, *, host: str = "127.0.0.1", port: int = 8765) -> "Server":
    """Starts the console and returns the running server (caller controls

    its lifetime, e.g. ``async with serve_console(...):`` or explicit
    ``.close()``). Protocol, both directions, is plain JSON lines:

    Server -> client, unprompted or on connect:
      {"type": "pending", "request": {...InterventionRequest...}}
      {"type": "idle", "control": "automation"|"human"}

    Client -> server:
      {"action": "resume", "note": "<what the operator did>"}
      {"action": "status"}
    """
    if websockets is None:
        raise RuntimeError("the 'websockets' package is required for the escalation console")

    async def handler(connection: "ServerConnection") -> None:
        await _handle_client(connection, controller)

    return await websockets.serve(handler, host, port)


async def run_console_forever(controller: EscalationController, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Convenience entry point for a standalone demo process."""
    server = await serve_console(controller, host=host, port=port)
    async with server:
        await asyncio.Future()  # run until cancelled
