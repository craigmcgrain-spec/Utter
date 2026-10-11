"""How a refusal or a failure reaches the user without a modal dialog (spec §9)."""

import logging

from PyQt6.QtWidgets import QSystemTrayIcon

logger = logging.getLogger(__name__)

TIMEOUT_MS = 5000
"""How long a tray balloon stays up, in milliseconds."""


def notify(app: QSystemTrayIcon | None, message: str, *, title: str = "Utter") -> None:
    """Show ``message`` as a tray balloon on ``app``.

    ``app`` is the tray icon to show it on. ``None`` means there is no tray to show it
    on -- a headless session or a test -- and the message is dropped rather than
    raising: a notification must never be the thing that kills the app, and a machine
    with no StatusNotifier host has nowhere to show it anyway.
    """
    if app is None:
        logger.debug("no tray icon to show %r on", message)
        return
    app.showMessage(title, message, app.icon(), TIMEOUT_MS)
