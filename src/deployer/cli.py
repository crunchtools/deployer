"""Command line entry point.

The verbs are deliberately few, and each one is something a person might also
need to do by hand during an incident:

``run``
    Drain the queue. Takes a lock, so two of these cannot overlap.
``rollback <service>``
    Put a service back on its rollback image without waiting for a ticket.
``unhold <service>``
    Clear a hold once the fix has shipped. The only way a hold ever clears
    other than the service verifying on a later ticket.
``holds``
    What is held, since when, and for which ticket.

``--dry-run`` is how this is exercised on a live host without touching a unit.

Where the registry and the state directory live is a property of the
deployment, not of this program, so both come from the environment
(``DEPLOYER_CONFIG`` and ``DEPLOYER_STATE``) and the unit supplies them. There
is no built-in path: a missing one exits 2 -- "could not run" -- rather than
guessing at a location and reporting on whatever happens to be there.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from . import __version__

EXIT_OK = 0
EXIT_ERROR = 1
# "Could not run": bad arguments, missing or invalid config, or a precondition
# that is not met. The caller needs this distinct from a failed deploy.
EXIT_COULD_NOT_RUN = 2

CONFIG_ENV = "DEPLOYER_CONFIG"
STATE_ENV = "DEPLOYER_STATE"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deployer",
        description="Drain the RT Deploy queue: deploy by commit, verify, roll back once.",
    )
    parser.add_argument("--version", action="version", version=f"deployer {__version__}")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Say what would happen and change nothing. No pull, no tag, no restart.",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get(CONFIG_ENV),
        help=f"Service registry path (default: ${CONFIG_ENV})",
    )
    parser.add_argument(
        "--state",
        default=os.environ.get(STATE_ENV),
        help=f"State directory holding holds, attempts and ticket state (default: ${STATE_ENV})",
    )

    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("run", help="Drain the Deploy queue, oldest ticket first")

    p_rollback = sub.add_parser("rollback", help="Put a service back on its rollback image")
    p_rollback.add_argument("service")

    p_unhold = sub.add_parser("unhold", help="Clear a service's hold")
    p_unhold.add_argument("service")

    sub.add_parser("holds", help="List what is held, since when, and for which ticket")
    return parser


def missing_paths(args: argparse.Namespace) -> list[str]:
    """Names of the required path settings that were given neither way."""
    return [name for name, value in ((CONFIG_ENV, args.config), (STATE_ENV, args.state)) if not value]


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and dispatch. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    absent = missing_paths(args)
    if absent:
        print(
            f"deployer: no {' and no '.join(absent)}; set the variable or pass the flag",
            file=sys.stderr,
        )
        return EXIT_COULD_NOT_RUN

    # Each verb lands in a later item; A1 is the scaffold, and a verb that
    # silently did nothing would be worse than one that says it is not wired.
    print(f"deployer {__version__}: {args.verb} is not implemented yet", file=sys.stderr)
    return EXIT_COULD_NOT_RUN


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
