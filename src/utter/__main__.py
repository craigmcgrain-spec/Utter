"""``python -m utter`` and the ``utter`` console script: the tray app, or one CLI command.

The two entry points are dispatched here, and the order matters for the first one: a desktop
shortcut or the tray starts the application with no arguments, and the portal's app-id has to
be claimed **before PyQt is imported** (spec §6.1 step 1 -- the registration must be the
process's first portal call). So the hotkey module is imported, ``register_with_portal()``
runs, and only then is ``utter.ui.app`` imported, which is what drags Qt in.

An invocation that carries arguments is a CLI command (``utter speak``, ``utter status``, …)
and never touches Qt: it delegates to the running instance over the socket, and ``cli.main``
owns the cold fallback. Nothing in this module knows a command name.
"""

import sys


def main() -> int:
    """Run the tray application when given no arguments, a CLI command otherwise."""
    argv = sys.argv[1:]
    if argv:
        from . import cli

        return cli.main(argv)

    # Imported here rather than at module level: Qt must not be imported before the portal
    # registration below, and the CLI path above must not pay for either. A registration
    # failure is logged by the hotkey module itself and is non-fatal -- an unbound backend
    # then tells the user how to bind `utter speak` by hand (spec §6.3).
    from .hotkey import register_with_portal

    register_with_portal()

    from .ui.app import run

    return run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
