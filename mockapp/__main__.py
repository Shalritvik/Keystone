"""Run the mock servicing console.

    python -m mockapp [--port 8800] [--host 127.0.0.1]

Binds to localhost by default. This application has no authentication and
deliberately renders exploitable-looking legacy markup; it must not be exposed
on a network interface.
"""

from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(prog="mockapp")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            f"refusing to bind to {args.host!r}: this mock has no authentication "
            "and is for local use only"
        )
        raise SystemExit(2)

    uvicorn.run(
        "mockapp.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
