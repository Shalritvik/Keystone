"""Manual replay verification tool -- stands in for `python -m keystone replay`

until the CLI exists (Phase 7). Loads an artifact, runs it once headless
against a live surface, and prints the ReplayResult.

Usage:
    python scripts/run_replay.py <capability_id> [--tenant T] [param=value ...]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from keystone.config import DEFAULT_ARTIFACT_DIR, Policy, Settings  # noqa: E402
from keystone.guardrails import GuardedSurface, Guardrails  # noqa: E402
from keystone.replay.engine import ReplayEngine  # noqa: E402
from keystone.schemas import CapabilityArtifact  # noqa: E402
from keystone.surface.web import PlaywrightSurface  # noqa: E402


async def main(capability_id: str, tenant: str | None, params: dict[str, str]) -> None:
    path = DEFAULT_ARTIFACT_DIR / f"{capability_id}.v1.json"
    artifact = CapabilityArtifact.model_validate(json.loads(path.read_text()))

    settings = Settings.from_env()
    policy = Policy.load(settings.policy_path)

    raw = await PlaywrightSurface.create(headless=settings.headless)
    guarded = GuardedSurface(raw, Guardrails(policy, attended=False))
    engine = ReplayEngine(guarded, settings.evidence_dir)
    try:
        result = await engine.run(artifact, params, tenant=tenant)
    finally:
        await guarded.close()

    print(json.dumps(result.model_dump(mode="json"), indent=2, default=str))
    print(f"\nSTATUS: {result.status}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python scripts/run_replay.py <capability_id> [--tenant T] [param=value ...]", file=sys.stderr)
        raise SystemExit(1)

    capability_id = sys.argv[1]
    tenant: str | None = None
    params: dict[str, str] = {}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--tenant":
            tenant = args[i + 1]
            i += 2
        elif "=" in args[i]:
            key, _, value = args[i].partition("=")
            params[key] = value
            i += 1
        else:
            i += 1

    asyncio.run(main(capability_id, tenant, params))
