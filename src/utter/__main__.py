"""``python -m utter`` and the ``utter`` console script: hand the command line to the CLI.

Deliberately thin: Task 13 grows this into the application entry point (it must register
with the portal *before* PyQt is imported), and everything the command line can do today
lives in :mod:`utter.cli`, which is testable without this module.
"""

import sys

from . import cli


def main() -> int:
    """Run the command line in ``sys.argv`` and return the process exit status."""
    return cli.main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
