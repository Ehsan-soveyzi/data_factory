"""Run unittest discovery with outbound connections disabled for every test."""

from contextlib import ExitStack
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


def run_tests(project_dir, *, stream=None, verbosity=1):
    """Discover the actual suite; any unmocked HTTP or socket access is an error."""
    project_dir = Path(project_dir).resolve()
    stream = stream or sys.stderr
    with ExitStack() as guards:
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection",
                       "socket.getaddrinfo"):
            guards.enter_context(patch(target, side_effect=AssertionError("Network disabled during tests")))
        suite = unittest.TestLoader().discover(str(project_dir / "tests"))
        return unittest.TextTestRunner(stream=stream, verbosity=verbosity).run(suite)


def main():
    result = run_tests(Path.cwd(), verbosity=2)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
