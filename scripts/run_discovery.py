"""Manual discovery verification tool -- stands in for `python -m keystone discover`

until the CLI exists (Phase 7).

Usage:
    python scripts/run_discovery.py "<goal>" --entry <url> [--tenant T]
        [--capability-id ID] [--verify-param key=value ...]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from keystone.discovery.agent import discover  # noqa: E402


async def main(goal: str, entry: str, tenant: str | None, capability_id: str, verify_params: dict[str, str]) -> None:
    outcome = await discover(
        goal, entry, capability_id=capability_id, tenant=tenant,
        verify_params=verify_params or None,
    )

    print(f"\nOK: {outcome.ok}", file=sys.stderr)
    print(f"REASON: {outcome.reason}", file=sys.stderr)
    print(f"DISCOVERY RUN: {outcome.discovery_run_id}", file=sys.stderr)
    if outcome.verification_run_id:
        print(f"VERIFICATION RUN: {outcome.verification_run_id}", file=sys.stderr)
    if outcome.artifact_path:
        print(f"ARTIFACT SAVED: {outcome.artifact_path}", file=sys.stderr)

    if outcome.artifact is not None:
        print(json.dumps(outcome.artifact.model_dump(mode="json"), indent=2, default=str))


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__, file=sys.stderr)
        raise SystemExit(1)

    goal = args[0]
    entry: str | None = None
    tenant: str | None = None
    capability_id = "discovered_capability"
    verify_params: dict[str, str] = {}

    i = 1
    while i < len(args):
        if args[i] == "--entry":
            entry = args[i + 1]
            i += 2
        elif args[i] == "--tenant":
            tenant = args[i + 1]
            i += 2
        elif args[i] == "--capability-id":
            capability_id = args[i + 1]
            i += 2
        elif args[i] == "--verify-param":
            key, _, value = args[i + 1].partition("=")
            verify_params[key] = value
            i += 2
        else:
            i += 1

    if not entry:
        print("error: --entry is required", file=sys.stderr)
        raise SystemExit(1)

    asyncio.run(main(goal, entry, tenant, capability_id, verify_params))
