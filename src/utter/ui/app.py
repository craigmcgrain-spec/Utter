"""The composition root: build the process's objects, serve the socket, then run the tray.

This is the only module that wires the engine to the GUI. It owns the process lifetime --
the model store, the player, the service, the tray, the overlay, the control server and the
hotkey backend are all built here and torn down here -- and it is the server side of the
control protocol the CLI's docstring specifies. The leaf UI modules (``tray``, ``overlay``,
``notify``) stay free of the engine; this module is allowed to import it.

**One instance.** :func:`run` first asks the socket whether another instance is already
serving (``{"command": "show"}``); if one answers, this process tells it to show itself and
exits 0 rather than starting a second tray over the same output device. The probe and the
bind are two steps, so :meth:`ControlServer.start` can still lose the race to a process that
bound in between; that ``RuntimeError`` is the same delegation and is treated as one. The
model is verified before Qt is built at all, so a missing model is a one-line message rather
than a window that opens and dies.

**The worker thread never touches a widget.** ``SynthService.on_state`` may fire on the
engine's worker thread, so the callback handed to it is :func:`build_state_bridge`'s
``changed`` signal, emitted from wherever the engine calls it and delivered to the tray and
the overlay as a *queued* call on the GUI thread -- the thread the bridge, the widgets and
the connection were made on. Hotkey callbacks arrive on a backend thread for the same
reason and go through a second relay, so the selection read (which touches the clipboard
through Qt) also happens on the GUI thread.

**A handler never raises.** The socket server turns an escaping exception into a response,
but it logs it as a handler bug; an over-long selection is an ordinary outcome, so the
``speak`` handler answers it itself (Task 9's contract). ``read_selection`` is given the
*configured* ``max_chars``: capture's own default is the literal 20000.

**The no-hotkey notice keys on nothing being bound.** A ``"none"`` backend, a declined
portal dialog, a session bus that cannot be opened and a listener that never started all
leave the backend's ``bound`` list empty, and each needs the manual ``utter speak`` binding
instructions (spec §6.3) -- so :func:`configure_hotkey` notifies whenever ``bound`` is empty
after ``start()``, not on the backend's name.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Callable

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from .. import paths
from ..capture import SelectionTooLong, read_selection
from ..config import Config, Hotkey
from ..control import ControlServer, send
from ..hotkey import MANUAL_INSTRUCTIONS, make_backend, select_backend
from ..logging import setup_logging
from ..tts.audio import Player
from ..tts.chunk import TooLongError
from ..tts.engine import SynthService
from ..tts.model import MODEL_NAME, VOICES_NAME, ModelError, ModelStore
from ..tts.phonemes import make_phonemizer
from .notify import notify
from .overlay import Overlay
from .tray import Tray

logger = logging.getLogger(__name__)


class _StateBridge(QObject):
    """The one place the engine's callback becomes a Qt signal.

    The service captures its callback at construction, so this object has to exist before
    the service does. It is created on the GUI thread; a widget that connects to it is
    therefore updated by a queued call, whatever thread ``changed`` is emitted from.
    """

    changed = pyqtSignal(str)
    """A playback-state transition: ``"PLAYING"``, ``"PAUSED"`` or ``"IDLE"``."""


class _ActionRelay(QObject):
    """Marshals a hotkey action from the backend's thread onto the GUI thread.

    A backend dispatches on its own thread (the portal's worker, the X11 event thread), so
    the relay is connected -- and therefore owned -- on the GUI thread; ``action`` is passed
    to the backend as its ``on_action``, and the connection delivers the dispatch where the
    clipboard and the widgets live.
    """

    action = pyqtSignal(str)
    """``"speak"``, ``"stop"`` or ``"pause"``, as the desktop named it."""


def build_state_bridge() -> _StateBridge:
    """Build the QObject whose ``changed`` signal marshals ``on_state`` onto the GUI thread."""
    return _StateBridge()


# -- the control protocol --------------------------------------------------


def build_handlers(
    service: SynthService, config: Config
) -> dict[str, Callable[[dict], dict]]:
    """The server side of Task 9's protocol: one call per command, no command knowledge here.

    ``doctor`` and ``hotkey`` are deliberately absent -- they are Task 16's. ``show`` is the
    acknowledgement a second invocation delegates to: it exists so ``utter`` with a live
    instance gets a real answer and exits 0.
    """

    def speak(request: dict) -> dict:
        return _speak(service, config, request)

    def stop(request: dict) -> dict:
        service.stop()
        return {"ok": True}

    def pause(request: dict) -> dict:
        service.pause()
        return {"ok": True}

    def resume(request: dict) -> dict:
        service.resume()
        return {"ok": True}

    def status(request: dict) -> dict:
        # The five keys at the top level, not nested: the CLI prints this object as it is.
        return {"ok": True, **service.status()}

    def voices(request: dict) -> dict:
        return {
            "ok": True,
            "voices": service.voice_names(),
            "active": config.speech.voice,
        }

    def show(request: dict) -> dict:
        return {"ok": True}

    return {
        "speak": speak,
        "stop": stop,
        "pause": pause,
        "resume": resume,
        "status": status,
        "voices": voices,
        "show": show,
    }


def _speak(service: SynthService, config: Config, request: dict) -> dict:
    """Speak the request's ``text``, or read the selection when it carries none.

    An over-long selection and an over-long text are answers, not exceptions: they leave the
    handler as ``{"ok": False, "error": ...}`` so the socket server never logs them as a bug
    in its handler, and so a hotkey dispatch can read the same response.
    """
    try:
        text = request.get("text")
        if text is None:
            selection = read_selection(max_chars=config.speech.max_chars)
            if selection is None:
                return {"ok": False, "error": "no text to speak: nothing is selected"}
            text = selection.text
        service.speak(text)
    except (SelectionTooLong, TooLongError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


def _hotkey_action(
    service: SynthService, config: Config, notifier: object
) -> Callable[[str], None]:
    """Dispatch one hotkey action, reporting a refused selection on the tray.

    Kept trivial on purpose: it runs as a queued GUI-thread call and a raising slot would be
    the engine's callback that broke, not an actionable error.
    """

    def dispatch(action: str) -> None:
        if action == "speak":
            response = _speak(service, config, {})
            if not response["ok"]:
                notify(notifier, response["error"])
        elif action == "stop":
            service.stop()
        elif action == "pause":
            # One action, both directions: "Pause or resume" is what the desktop binds.
            service.resume() if service.status().get("paused") else service.pause()
        else:
            logger.warning("ignoring unknown hotkey action %r", action)

    return dispatch


# -- hotkeys ---------------------------------------------------------------


_backend: object | None = None
"""The backend :func:`configure_hotkey` started, so :func:`stop_hotkey` can release it.

A module global because the contract's return value is the backend's *name*; ``run`` still
has to close the portal session (which releases the desktop's grab) on the way out.
"""


def configure_hotkey(
    config: Hotkey,
    service: SynthService,
    *,
    session_type: str,
    portal_available: bool,
    notifier: object = None,
) -> str:
    """Start the hotkey backend ``config`` selects and return its name.

    ``notifier`` is the tray the manual-binding instructions are shown on (``None`` drops
    them, which is what a headless caller wants). The instructions go up whenever nothing is
    bound after ``start()`` -- a ``"none"`` backend, a declined dialog, an unreachable bus
    and a listener that never started all look the same from here, and all of them mean the
    user has to bind ``utter speak`` themselves (spec §6.3).
    """
    global _backend
    name = select_backend(
        config, session_type=session_type, portal_available=portal_available
    )
    backend = make_backend(name, config)
    if backend is None:
        _no_hotkey_notice(notifier)
        return name
    relay = _ActionRelay()
    relay.action.connect(_hotkey_action(service, config, notifier))
    _keep_alive(relay)
    backend.start(relay.action.emit)
    if getattr(backend, "bound", None) == []:
        _no_hotkey_notice(notifier)
    _backend = backend
    return name


def stop_hotkey() -> None:
    """Stop the backend :func:`configure_hotkey` started; safe when there is none."""
    global _backend
    backend, _backend = _backend, None
    if backend is not None:
        backend.stop()


def _no_hotkey_notice(notifier: object) -> None:
    notify(notifier, MANUAL_INSTRUCTIONS)


def _keep_alive(relay: _ActionRelay) -> None:
    """Parent ``relay`` to the application so its queued deliveries keep arriving.

    Without an owner CPython would collect the relay -- and with it the connection -- as
    soon as ``configure_hotkey`` returns, and every later hotkey press would be dropped.
    """
    application = QApplication.instance()
    if application is not None:
        relay.setParent(application)


# -- the single instance ---------------------------------------------------


def delegate_if_running() -> bool:
    """Ask the instance that owns the socket to show itself; True when one answered."""
    return send({"command": "show"}) is not None


# -- the application -------------------------------------------------------


def run(argv: list[str] | None = None) -> int:
    """Start the tray application and return the process exit status.

    Zero when this process delegated to a live instance. Otherwise the Qt event loop runs
    until the tray's Quit is chosen (or the session ends), and every thread and socket this
    function created is released before it returns.
    """
    if delegate_if_running():
        logger.info("another utter instance owns %s; asked it to show", paths.socket_path())
        return 0

    config = Config.load().merged_with_env()
    setup_logging(config.general.log_level)

    try:
        store = ModelStore(
            paths.models_dir(),
            {
                MODEL_NAME: config.install.model_sha256,
                VOICES_NAME: config.install.voices_sha256,
            },
        )
        store.verify()
    except ModelError as exc:
        print(f"utter: {exc}", file=sys.stderr)
        print("utter: run the installer, or `utter doctor`, to install the model", file=sys.stderr)
        return 1

    player = Player()  # constructing it opens nothing; start() opens the device
    try:
        player.start()
    except Exception as exc:  # sounddevice's own error type, and anything like it
        # shutdown() is safe on a half-started player: it closes a stream that opened
        # before the failure and joins nothing that was never started.
        player.shutdown()
        print(f"utter: cannot open the audio output: {exc}", file=sys.stderr)
        return 1

    application = QApplication(argv if argv is not None else [])
    # Nothing this app shows is a window in the quit-on-close sense: the overlay hides and
    # shows while the tray keeps running.
    application.setQuitOnLastWindowClosed(False)

    bridge = build_state_bridge()

    notifier: list[object] = []
    """The tray, once it exists: the engine's error callback and the hotkey notice read it."""

    service = SynthService(
        model=store,
        player=player,
        speech=config.speech,
        phonemizer=make_phonemizer(config.speech.phonemizer),
        on_state=bridge.changed.emit,
        on_error=lambda message: notify(notifier[0] if notifier else None, message),
    )

    tray = Tray(service, config, on_quit=application.quit)
    notifier.append(tray)
    overlay = Overlay(service, config)
    # Queued on the GUI thread: on_state may be emitted by the engine's worker.
    bridge.changed.connect(tray.set_state)
    bridge.changed.connect(overlay.show_for_state)
    overlay.speed_committed.connect(_apply_speed(config, tray))

    server = ControlServer(paths.socket_path(), build_handlers(service, config))
    try:
        server.start()
    except RuntimeError as exc:
        # A live instance (or one that got there first) owns the socket: the same delegation
        # delegate_if_running() does, just after the race was lost.
        logger.info("delegating to the instance that owns the socket: %s", exc)
        service.shutdown()
        player.shutdown()
        return 0

    tray.show()
    try:
        configure_hotkey(
            config.hotkey,
            service,
            session_type=os.environ.get("XDG_SESSION_TYPE", ""),
            portal_available=_portal_available(),
            notifier=tray,
        )
        return application.exec()
    finally:
        stop_hotkey()
        server.stop()
        service.shutdown()
        player.shutdown()


def _apply_speed(config: Config, notifier: object) -> Callable[[float], None]:
    """Report a speed the overlay committed: the engine reads it from the next unit on.

    The service holds ``config.speech`` itself, so writing the field is what changes the
    speed; the save makes it survive a restart. An unwritable config is reported, not raised:
    the speed is already in effect and losing the file must not break the session.
    """

    def apply(speed: float) -> None:
        config.speech.speed = speed
        try:
            config.save()
        except OSError as exc:
            notify(notifier, f"Could not save settings: {exc}")

    return apply


def _portal_available() -> bool:
    """Cheapest evidence the desktop portal can be reached: a session bus to call it on.

    A real interface probe is a blocking D-Bus round trip on the GUI thread at startup; the
    backend is where the truth arrives, because a bind that fails there leaves ``bound``
    empty and the user gets the manual instructions either way.
    """
    return bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS"))
