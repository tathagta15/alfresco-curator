"""Console entry point: `alfresco-curator` or `python -m alfresco_curator`."""

from __future__ import annotations

import argparse

import uvicorn

from alfresco_curator import __version__, logging_setup
from alfresco_curator.config import get_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="alfresco-curator",
        description="Serve the Alfresco Curator.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8600)
    parser.add_argument("--reload", action="store_true", help="Reload on source changes.")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    settings = get_settings()
    logging_setup.configure(settings.web.log_level)

    uvicorn.run(
        "alfresco_curator.web.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=settings.web.log_level.lower(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
