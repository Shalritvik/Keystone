"""Entry point for `python -m keystone <command>`."""

from __future__ import annotations

from keystone.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
