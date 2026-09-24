"""Open a URL and print what PlaywrightSurface perceives there.

There is no automated test that can tell you whether accessible-name
computation "looks right" -- a human has to read the tree. Run this against
both tenants whenever web.py's naming logic changes.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from grip.surface.base import ActionRequest  # noqa: E402
from grip.surface.web import PlaywrightSurface  # noqa: E402


async def main(url: str) -> None:
    surface = await PlaywrightSurface.create(headless=True)
    try:
        outcome = await surface.act(ActionRequest(action="navigate", url=url))
        if not outcome.ok:
            print(f"navigation failed: {outcome.detail}", file=sys.stderr)
            raise SystemExit(1)
        observation = await surface.observe()
        print(observation.render(max_nodes=500))
    finally:
        await surface.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python scripts/dump_ax.py <url>", file=sys.stderr)
        raise SystemExit(1)
    asyncio.run(main(sys.argv[1]))
