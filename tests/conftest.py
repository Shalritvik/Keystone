"""Shared fixtures for tests that need a live mock app + real browser.

Replay's whole point is deterministic *browser* interaction, so its
correctness cannot be meaningfully verified against a mock of the surface --
these fixtures start the real mock app once per test session and give each
test a fresh browser context (never reused across tests, per the multi-tenant
isolation rule in CLAUDE.md).
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_URL = "http://127.0.0.1:8800"


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) == 0


@pytest.fixture(scope="session")
def mockapp_server():
    """Starts `python -m mockapp` once for the whole test session, unless

    something is already listening on 8800 (a developer running it manually
    during `pytest -k replay`), in which case that instance is reused and
    left running afterward.
    """
    already_running = _port_open("127.0.0.1", 8800)
    proc: subprocess.Popen | None = None
    if not already_running:
        proc = subprocess.Popen(
            [sys.executable, "-m", "mockapp"],
            cwd=REPO_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            if _port_open("127.0.0.1", 8800):
                break
            time.sleep(0.1)
        else:
            proc.terminate()
            raise RuntimeError("mock app did not start listening on 127.0.0.1:8800")

    yield BASE_URL

    if proc is not None:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.fixture
def clear_faults(mockapp_server):
    """Every test starts from a clean fault board, and cleans up after

    itself even if it armed something and then failed before disarming it.
    """
    httpx.post(f"{mockapp_server}/_faults/clear")
    yield
    httpx.post(f"{mockapp_server}/_faults/clear")


@pytest_asyncio.fixture
async def guarded_surface(clear_faults):
    """One browser context per test -- never reused, per CLAUDE.md."""
    from grip.config import Policy
    from grip.guardrails import GuardedSurface, Guardrails
    from grip.surface.web import PlaywrightSurface

    raw = await PlaywrightSurface.create(headless=True)
    policy = Policy.load(REPO_ROOT / "policy.yaml")
    guarded = GuardedSurface(raw, Guardrails(policy, attended=False))
    try:
        yield guarded
    finally:
        await guarded.close()


@pytest_asyncio.fixture
async def surface(clear_faults):
    """The raw (unwrapped) PlaywrightSurface -- for tests exercising

    resolve()/perception directly, below the guardrail layer.
    """
    from grip.surface.web import PlaywrightSurface

    s = await PlaywrightSurface.create(headless=True, action_timeout_s=5.0)
    try:
        yield s
    finally:
        await s.close()
