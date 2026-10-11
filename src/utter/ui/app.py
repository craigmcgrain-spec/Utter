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

**The worker thread never touches a widget.** ``SynthService.on_state`` and its ``on_error``
are both reached from the engine's worker thread, so the callbacks handed to it are
:func:`build_state_bridge`'s ``changed`` and ``error`` signals, emitted from wherever the
engine calls them and delivered to the tray and the overlay as *queued* calls on the GUI
thread -- the thread the bridge, the widgets and the connections were made on. Hotkey
callbacks arrive on a backend thread for the same reason and go through a second relay, so
the selection read (which touches the clipboard through Qt) also happens on the GUI thread.

**A text-less ``speak`` reads on the GUI thread too.** The handler runs on a control server's
per-connection thread, so it hands the read to :class:`_GuiSelectionReader`, a blocking
round trip onto the GUI thread with a bounded wait: the answer is the same selection, and a
GUI thread that never answers produces an error response instead of a hung connection.

**A handler never raises.** The socket server turns an escaping exception into a response,
but it logs it as a handler bug; an over-long selection is an ordinary outcome, so the
``speak`` handler answers it itself (Task 9's contract). ``read_selection`` is given the
*configured* ``max_chars``: capture's own default is the literal 20000.

**The no-hotkey notice keys on nothing being bound.** A ``"none"`` backend can say so at
once, but a portal bind concludes asynchronously -- the desktop answers a dialog whenever the
user gets to it, and ``bound`` is still empty until it does -- so the app cannot decide at
``start()``. The backend reports the outcome through its ``on_unbound`` hook instead, exactly
once, from its worker thread, and the wiring delivers it as a queued GUI-thread notice. The
spec §6.3 manual ``utter speak`` instructions go up on that outcome, whatever the reason
(refusal, empty result, bus error, timeout, or a listener that never started), and never on a
bind that succeeded.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Callable

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from .. import paths
from ..capture import Selection, SelectionTooLong, read_selection
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
from .tray import Tray, _set_speed

logger = logging.getLogger(__name__)

SELECTION_TIMEOUT = 5.0
"""Seconds a socket thread waits for its GUI-thread selection read before answering an error.

Comfortably over capture's own bound on the provider it may run (``WL_PASTE_TIMEOUT`` is
2.0 s for ``wl-paste``), so a healthy read is never cut short; what it guards against is a GUI
thread that is not pumping at all, where the alternative is a connection hanging open."""


class _StateBridge(QObject):
    """The one place the engine's callbacks become Qt signals.

    The service captures its callbacks at construction, so this object has to exist before
    the service does. It is created on the GUI thread; a widget -- or a slot that touches one
    -- connected here is therefore woken by a queued call, whatever thread the signal was
    emitted from. That is what keeps ``QSystemTrayIcon.showMessage`` off the ``utter-synth``
    worker, which is where both ``on_state`` and ``on_error`` are reached from.
    """

    changed = pyqtSignal(str)
    """A playback-state transition: ``"PLAYING"``, ``"PAUSED"`` or ``"IDLE"``."""

    error = pyqtSignal(str)
    """A synthesis fault, reported by the engine's worker thread."""


class _HotkeyBridge(QObject):
    """Marshals a hotkey backend's callbacks from its own thread onto the GUI thread.

    A backend dispatches actions and reports an unbound bind on its own thread (the portal's
    worker, the X11 event thread), so this object is created -- and its connections made --
    on the GUI thread, and both signals are handed to the backend as its callbacks: the
    queued delivery is what puts the selection read (which touches the clipboard through Qt)
    and the tray notice where they belong.
    """

    action = pyqtSignal(str)
    """``"speak"``, ``"stop"`` or ``"pause"``, as the desktop named it."""

    unbound = pyqtSignal()
    """The bind concluded with nothing bound: the manual instructions are needed."""


class _SelectionRead:
    """One selection read asked of the GUI thread, and where its answer goes."""

    def __init__(self, max_chars: int) -> None:
        self.max_chars = max_chars
        self.selection: Selection | None = None
        self.error: BaseException | None = None
        self.done = threading.Event()


class _GuiSelectionReader(QObject):
    """Reads the selection on the GUI thread for a caller on a socket thread.

    ``speak`` without ``text`` runs on a control server's per-connection thread, but the read
    ends in ``QApplication.clipboard()`` on X11 -- a GUI-thread object -- so it is marshalled
    here exactly as the engine's callbacks are. The asking thread waits on an event with a
    deadline: a GUI thread that is not pumping answers with a timeout error rather than
    leaving the connection hanging open.
    """

    _ask = pyqtSignal(object)
    """Carries one :class:`_SelectionRead`; queued when the asker is not the GUI thread."""

    def __init__(self) -> None:
        super().__init__()
        self._ask.connect(self._answer)

    def selection(self, *, max_chars: int, timeout: float) -> Selection | None:
        """Return the selection read on this object's thread, or raise what the read raised."""
        asked = _SelectionRead(max_chars)
        self._ask.emit(asked)
        if not asked.done.wait(timeout):
            raise TimeoutError(f"the selection was not read within {timeout:.1f}s")
        if asked.error is not None:
            raise asked.error
        return asked.selection

    def _answer(self, asked: _SelectionRead) -> None:
        """Read on the GUI thread; anything that escapes travels back to the asker."""
        try:
            asked.selection = read_selection(max_chars=asked.max_chars)
        except BaseException as exc:  # SelectionTooLong included: the asker re-raises it
            asked.error = exc
        finally:
            asked.done.set()


def build_state_bridge() -> _StateBridge:
    """Build the QObject whose ``changed`` and ``error`` signals marshal the engine's callbacks."""
    return _StateBridge()


# -- the control protocol --------------------------------------------------


def build_handlers(
    service: SynthService,
    config: Config,
    *,
    read: Callable[..., Selection | None] | None = None,
) -> dict[str, Callable[[dict], dict]]:
    """The server side of Task 9's protocol: one call per command, no command knowledge here.

    ``read`` is where a text-less ``speak`` reads the selection. It defaults to
    :func:`utter.capture.read_selection` on the calling thread; ``run()`` passes a GUI-thread
    round trip instead, because the handler runs on a control-server connection thread and the
    read ends in ``QApplication.clipboard()`` on X11.

    ``doctor`` and ``hotkey`` are deliberately absent -- they are Task 16's. ``show`` is the
    acknowledgement a second invocation delegates to: it exists so ``utter`` with a live
    instance gets a real answer and exits 0.
    """
    reader = read_selection if read is None else read

    def speak(request: dict) -> dict:
        return _speak(service, config, request, reader)

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


def _speak(
    service: SynthService,
    config: Config,
    request: dict,
    read: Callable[..., Selection | None],
) -> dict:
    """Speak the request's ``text``, or read the selection when it carries none.

    An over-long selection, an over-long text and a selection that could not be read in time
    are answers, not exceptions: they leave the handler as ``{"ok": False, "error": ...}`` so
    the socket server never logs them as a bug in its handler, and so a hotkey dispatch can
    read the same response.
    """
    try:
        text = request.get("text")
        if text is None:
            selection = read(max_chars=config.speech.max_chars)
            if selection is None:
                return {"ok": False, "error": "no text to speak: nothing is selected"}
            text = selection.text
        service.speak(text)
    except (SelectionTooLong, TooLongError) as exc:
        return {"ok": False, "error": str(exc)}
    except TimeoutError as exc:
        # The GUI-thread read did not come back, which is a read that failed, not a bug: the
        # connection gets an answer instead of hanging, and no second engine is invented.
        return {"ok": False, "error": f"could not read the selection: {exc}"}
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
            # A direct read: this dispatch already runs as a queued GUI-thread call.
            response = _speak(service, config, {}, read_selection)
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


def _connect_engine(
    bridge: _StateBridge, config: Config, tray: Tray, overlay: Overlay
) -> None:
    """Wire the engine's marshalled callbacks and the overlay's controls into the widgets.

    Every connection is made here, on the GUI thread the widgets live on. ``changed`` and
    ``error`` may be emitted by the engine's worker, so their delivery is queued; the overlay's
    committed speed goes to the tray's own setter, the one place that writes the field and
    persists it, so the menu and the slider cannot drift apart in how they save.
    """
    bridge.changed.connect(tray.set_state)
    bridge.changed.connect(overlay.show_for_state)
    bridge.error.connect(lambda message: notify(tray, message))
    overlay.speed_committed.connect(lambda speed: _set_speed(config, speed, tray))


# -- hotkeys ---------------------------------------------------------------


_backend: object | None = None
"""The backend :func:`configure_hotkey` started, so :func:`stop_hotkey` can release it.

A module global because the contract's return value is the backend's *name*; ``run`` still
has to stop the backend on the way out, which is what releases the desktop's grab.
"""


def configure_hotkey(
    config: Hotkey,
    service: SynthService,
    *,
    session_type: str,
    portal_available: bool,
    notifier: object,
) -> str:
    """Start the hotkey backend ``config`` selects and return its name.

    ``notifier`` is the tray the manual-binding instructions go on; it is required rather
    than optional because a silently-dropped ``None`` default would hide the very notice this
    function's second half exists to raise (a caller with no tray passes ``None`` on purpose).

    The instructions go up when **nothing was bound**, which a ``"none"`` backend can say at
    once (there is no bind to wait for) but a portal bind cannot: it concludes whenever the
    user answers the dialog, so the backend reports it through ``on_unbound``, and the notice
    is delivered as a queued call on the GUI thread. A bind that succeeds never notifies.
    """
    global _backend
    # A second call in one process (a reconfiguration, or a second run()) must not leak the
    # backend it replaces: leaving it running would hold a second portal session's grab.
    stop_hotkey()
    name = select_backend(
        config, session_type=session_type, portal_available=portal_available
    )
    backend = make_backend(name, config)
    if backend is None:
        _no_hotkey_notice(notifier)
        return name
    bridge = _HotkeyBridge()
    bridge.action.connect(_hotkey_action(service, config, notifier))
    bridge.unbound.connect(lambda: _no_hotkey_notice(notifier))
    _keep_alive(bridge)
    backend.start(bridge.action.emit, on_unbound=bridge.unbound.emit)
    _backend = backend
    return name


def stop_hotkey() -> None:
    """Stop the backend :func:`configure_hotkey` started; safe when there is none.

    ``run()`` calls this on its way out, and that is its whole purpose: stopping a
    ``PortalHotkey`` closes the portal session on the backend's own thread, which is what
    releases the desktop's grab instead of leaving it held until the process dies.
    """
    global _backend
    backend, _backend = _backend, None
    if backend is not None:
        backend.stop()


def _no_hotkey_notice(notifier: object) -> None:
    notify(notifier, MANUAL_INSTRUCTIONS)


def _keep_alive(bridge: _HotkeyBridge) -> None:
    """Parent ``bridge`` to the application so its queued deliveries keep arriving.

    Without an owner CPython would collect the bridge -- and with it the connections -- as
    soon as ``configure_hotkey`` returns, and every later hotkey press, and any later unbound
    report, would be dropped.
    """
    application = QApplication.instance()
    if application is not None:
        bridge.setParent(application)


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
    reader = _GuiSelectionReader()
    """The GUI-thread side of a text-less ``speak``; see :class:`_GuiSelectionReader`."""

    service = SynthService(
        model=store,
        player=player,
        speech=config.speech,
        phonemizer=make_phonemizer(config.speech.phonemizer),
        # Both are emitted by the engine's worker, so both go through the bridge: the queued
        # delivery is what keeps a widget (and a balloon) on the GUI thread.
        on_state=bridge.changed.emit,
        on_error=bridge.error.emit,
    )

    tray = Tray(service, config, on_quit=application.quit)
    overlay = Overlay(service, config)
    _connect_engine(bridge, config, tray, overlay)

    server = ControlServer(
        paths.socket_path(),
        build_handlers(
            service,
            config,
            read=lambda max_chars: reader.selection(
                max_chars=max_chars, timeout=SELECTION_TIMEOUT
            ),
        ),
    )
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


def _portal_available() -> bool:
    """Cheapest evidence the desktop portal can be reached: a session bus to call it on.

    A real interface probe is a blocking D-Bus round trip on the GUI thread at startup; the
    backend is where the truth arrives, because a bind that fails there leaves ``bound``
    empty and the user gets the manual instructions either way.
    """
    return bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS"))
