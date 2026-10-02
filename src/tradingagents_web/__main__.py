"""``tradingagents-web``: serve the web UI.

One server process only: the job scheduler lives in it, so ``--workers`` and
``--reload`` are not offered.
"""

from __future__ import annotations

import argparse
import ipaddress
import sys


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tradingagents-web", description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log-level", default="info")
    parser.add_argument("--allow-unauthenticated", action="store_true",
                        help="serve a non-loopback address without a token; only for a container "
                             "whose port is published on 127.0.0.1")
    args = parser.parse_args(argv)

    import uvicorn

    from tradingagents_web.app import create_app
    from tradingagents_web.settings import AppSettings

    settings = AppSettings()
    if not _is_loopback(args.host) and not settings.token and not args.allow_unauthenticated:
        sys.exit(f"Refusing to listen on {args.host} without a login: set TRADINGAGENTS_WEB_TOKEN "
                 "(or bind to 127.0.0.1).")
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level=args.log_level,
                workers=1)


if __name__ == "__main__":
    main()
