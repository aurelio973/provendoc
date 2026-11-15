"""``provendoc-serve`` -- REST API entry point (uvicorn).

Usage:
    provendoc-serve                          # 127.0.0.1:8000
    provendoc-serve --host 0.0.0.0 --port 8080
    python -m provendoc.api.server            # equivalent entry
"""

import argparse
import logging
import os


def _configure_logging() -> None:
    """Entry-point logging setup (library code stays NullHandler-silent).

    uvicorn's own logger is unaffected (its default logging config
    sets propagate=False). Defaults to INFO so batch lifecycle events
    (claim/finish/fail) and parse_failed audit warnings are visible;
    PROVENDOC_LOG_LEVEL overrides (e.g. DEBUG).
    """
    level = os.environ.get("PROVENDOC_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv=None) -> int:
    """CLI entry point: parse args, configure logging, serve uvicorn."""
    parser = argparse.ArgumentParser(
        prog="provendoc-serve",
        description="Serve the Provendoc REST API.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--reload", action="store_true",
        help="development auto-reload (implies 1 worker)",
    )
    args = parser.parse_args(argv)

    _configure_logging()

    import uvicorn

    uvicorn.run(
        "provendoc.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
