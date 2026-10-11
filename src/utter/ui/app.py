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

**Re-issuing the hotkey bind happens on the GUI thread as well.** ``hotkey --show`` arrives on a
connection thread, but a bind stops the running backend and builds a :class:`_HotkeyBridge`
parented to the application, so :class:`_GuiRebinder` hands the call over exactly as the reader
above does. The *wait* for the bind's outcome stays on the asker's thread: the outcome arrives
from the backend's own worker, and holding the GUI thread for it is the one thing that would stop
the desktop's dialog from being shown.

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
import time
from typing import Callable

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from .. import paths
from ..autostart import reconcile_autostart
from ..capture import Selection, SelectionTooLong, read_selection
from ..config import Config, Hotkey
from ..control import TIMEOUT as CLIENT_TIMEOUT, ControlServer, send
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

SELECTION_HEADROOM = 0.5
"""Seconds of the client's deadline reserved for the response to travel back to it.

:func:`utter.control.send` waits ``control.TIMEOUT`` seconds for a whole round trip, and a
client that gives up reads ``None`` -- "nothing is listening" -- which makes ``utter speak``
cold-start a second engine and say the text twice. So the reader's own deadline must sit
strictly below the client's, with room for the answer to get back before the client stops
listening."""

SELECTION_TIMEOUT = CLIENT_TIMEOUT - SELECTION_HEADROOM
"""Seconds a socket thread waits for its GUI-thread selection read before answering an error.

Derived from the *client's* deadline so the two cannot drift apart: the reader must always give
up first, because a client that gives up is not told "the read timed out", it is told nothing
answered. It still comfortably covers a healthy read -- the clipboard itself is immediate, and
capture's own ``wl-paste`` bound (2.0 s) is the only slow provider, whose worst case now ends in
an error answer rather than in a second engine."""

REBIND_HEADROOM = 0.5
"""Seconds of the client's deadline reserved for the bind's answer to travel back.

The same reasoning as :data:`SELECTION_HEADROOM`: :func:`utter.control.send` waits
``control.TIMEOUT`` seconds for a whole round trip and a client that gives up reads ``None``,
which ``hotkey --show`` turns into the manual instructions -- telling a user whose hotkeys are
bound to go and bind them by hand. So the whole rebind -- the GUI handoff, the join of the
backend it replaces and the wait for the new bind's outcome -- must conclude strictly before the
client stops listening."""

REBIND_TIMEOUT = CLIENT_TIMEOUT - REBIND_HEADROOM
"""Seconds the whole rebind may take, one budget shared by the handoff and by the outcome wait.

Derived from the *client's* deadline so the two cannot drift apart, and spent from a single
``time.monotonic()`` reading taken before the request is handed over. A second, fresh deadline for
the wait would double the budget and put the answer past the client's give-up point, where the
CLI cannot tell "the instance is slow" from "no instance at all". The work inside can still
overrun it -- ``configure_hotkey`` joins the backend it replaces, whose own bound is
``hotkey._STOP_TIMEOUT`` -- and then the request answers with its own failure before the client
gives up, which the CLI prints as the bind steps."""

REBIND_POLL_INTERVAL = 0.02
"""Seconds between two reads of a re-issued bind's state while it is being waited for."""


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


class _RebindAsk:
    """One hotkey bind asked of the GUI thread, and the backend it started."""

    def __init__(self) -> None:
        self.name: str = "none"
        self.backend: object | None = None
        self.error: BaseException | None = None
        self.done = threading.Event()


class _GuiRebinder(QObject):
    """Re-issues the hotkey bind on the GUI thread for a caller on a socket thread.

    ``hotkey --show`` runs on a control server's per-connection thread, but re-issuing a bind
    stops the running backend and builds a fresh :class:`_HotkeyBridge` parented to the
    application -- a Qt object belongs to the thread that made it -- so the call is marshalled
    here exactly as the engine's callbacks and the selection read are. The wait for the new
    bind's outcome stays on the asker's thread: the outcome arrives from the backend's own
    worker, and holding the GUI thread for it is the one thing that would stop the desktop's
    dialog from being shown at all.
    """

    _ask = pyqtSignal(object)
    """Carries one :class:`_RebindAsk`; queued when the asker is not the GUI thread."""

    def __init__(self, configure: Callable[[], str]) -> None:
        super().__init__()
        self._configure = configure
        self._ask.connect(self._run)

    def request(self) -> tuple[str, list[str], bool]:
        """Re-issue the bind and report ``(backend name, bound ids, concluded unbound)``.

        The ids are the ones the desktop confirmed, which the portal records as soon as its
        bind concludes; ``concluded unbound`` is the backend's own verdict that nothing can be
        delivered -- what the manual instructions exist to answer. A backend that reports
        neither (X11 grabs on its own thread and reports only failure) answers with an empty
        list and no verdict, and the caller then says the bind was re-issued rather than
        claiming an outcome it cannot see.

        One deadline covers both halves of the request, taken before the handoff: the GUI thread
        answers in milliseconds when it is free, and the wait for the outcome then gets what is
        left. Reading the clock again for that wait would hand the client an answer it has
        already stopped listening for.
        """
        deadline = time.monotonic() + REBIND_TIMEOUT
        asked = _RebindAsk()
        self._ask.emit(asked)
        if not asked.done.wait(max(deadline - time.monotonic(), 0.0)):
            raise TimeoutError("the application did not answer the rebind request")
        if asked.error is not None:
            raise asked.error
        return _bind_outcome(asked.name, asked.backend, deadline)

    def _run(self, asked: _RebindAsk) -> None:
        """Re-issue the bind on the GUI thread; anything that escapes travels back to the asker.

        A bind that raises must not escape into Qt's event loop: an unhandled exception in a
        slot is not an answer to anybody, and the socket connection is waiting on one.
        """
        try:
            asked.name = self._configure()
            asked.backend = _backend
        except BaseException as exc:  # PortalError included: an ordinary outcome of a bind
            asked.error = exc
        finally:
            asked.done.set()


def build_state_bridge() -> _StateBridge:
    """Build the QObject whose ``changed`` and ``error`` signals marshal the engine's callbacks."""
    return _StateBridge()


def _audio_error(bridge: _StateBridge) -> Callable[[str], None]:
    """The player's failure handler: relay a write failure onto the GUI thread as a notice.

    ``Player.on_error`` runs on the player's writer thread, so returning ``bridge.error.emit``
    unchanged is what puts the balloon where the other worker-thread callbacks go -- a queued
    call on the GUI thread. The message names the device rather than only echoing the
    exception, because the same signal also carries synthesis faults and the user has to know
    which one stopped the audio.
    """

    def report(message: str) -> None:
        bridge.error.emit(f"Audio device stopped: {message}")

    return report


# -- the control protocol --------------------------------------------------


def build_handlers(
    service: SynthService,
    config: Config,
    *,
    read: Callable[..., Selection | None] | None = None,
    rebind: Callable[[], tuple[str, list[str], bool]] | None = None,
) -> dict[str, Callable[[dict], dict]]:
    """The server side of Task 9's protocol: one call per command, no command knowledge here.

    ``read`` is where a text-less ``speak`` reads the selection. It defaults to
    :func:`utter.capture.read_selection` on the calling thread; ``run()`` passes a GUI-thread
    round trip instead, because the handler runs on a control-server connection thread and the
    read ends in ``QApplication.clipboard()`` on X11. The default exists for this function's own
    tests -- ``run()`` is the only intended caller and goes through :func:`served_handlers`.

    ``rebind`` re-issues the hotkey bind for ``hotkey --show``, with the same split and for the
    same reason: it defaults to a plain call for this function's own tests, and ``run()`` passes
    a GUI-thread round trip in.

    ``doctor`` is deliberately absent: a diagnosis is answered by the CLI itself, because its
    every input -- the config, the pack, the log, the session -- is on the disk the CLI runs
    from, and it has to work when no instance is running. ``show`` is the acknowledgement a
    second invocation delegates to: it exists so ``utter`` with a live instance gets a real
    answer and exits 0.
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

    def hotkey(request: dict) -> dict:
        """Re-issue the bind and answer with what it produced (spec §6.1, §8.3).

        ``show`` is the whole command: re-issuing the bind is what makes the desktop show its
        shortcut dialog (or re-confirm the shortcuts it already holds). The answer is the
        bind's own outcome -- the ids the desktop confirmed, or the manual instructions when
        nothing was bound -- because only the app can see it: a CLI reading the session type
        would be guessing, and guessing wrong is what sends a user to bind keys by hand that
        were already bound.
        """
        if not request.get("show"):
            return {"ok": True}
        if rebind is None:
            return {"ok": False, "error": "this instance cannot re-issue the hotkey bind"}
        try:
            name, shortcuts, unbound = rebind()
        except Exception as exc:
            # An answer, not a hang and not a handler bug: a bind that failed is an ordinary
            # outcome of asking for one, and the client is waiting on this connection.
            return {"ok": False, "error": f"could not re-issue the hotkey bind: {exc}"}
        return {
            "ok": True,
            "backend": name,
            "shortcuts": shortcuts,
            "instructions": MANUAL_INSTRUCTIONS if unbound else "",
        }

    return {
        "speak": speak,
        "stop": stop,
        "pause": pause,
        "resume": resume,
        "status": status,
        "voices": voices,
        "show": show,
        "hotkey": hotkey,
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


def served_handlers(
    service: SynthService, config: Config, *, notifier: object | None = None
) -> dict[str, Callable[[dict], dict]]:
    """The handler table ``run()`` serves: :func:`build_handlers` with the GUI-thread round trips.

    Kept apart from ``run()`` so a test can serve the table the application really serves. The
    ``read`` default :func:`build_handlers` keeps is for its own unit tests; ``run()`` is its
    only intended caller, and it always comes through here -- a caller that forgets the reader
    would silently read the clipboard off the socket's thread again, which is what the reader
    exists to prevent. The same goes for the rebinder: a re-issued bind builds Qt objects, so it
    belongs on the GUI thread too.

    ``notifier`` is the tray a re-issued bind's manual instructions go on; ``None`` drops the
    notice, which is what a caller with no tray passes on purpose.
    """
    reader = _GuiSelectionReader()
    rebinder = _GuiRebinder(lambda: bind_hotkeys(service, config, notifier))
    return build_handlers(
        service,
        config,
        read=lambda max_chars: reader.selection(
            max_chars=max_chars, timeout=SELECTION_TIMEOUT
        ),
        rebind=rebinder.request,
    )


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

_unbound_reported = False
"""Whether the bind in place concluded with nothing bound.

The portal reports the ids it bound, but an X11 backend grabs on its own thread and reports only
failure -- through ``on_unbound`` -- so this flag is the one outcome both backends agree on, and
it is what ``utter hotkey --show`` answers with when there is no list of ids to show. It is
cleared when a bind starts, so it always describes the bind that is current.
"""


def bind_hotkeys(service: SynthService, config: Config, notifier: object) -> str:
    """Issue the hotkey bind for this session and return the backend's name.

    The one place the bind is invoked: the tray's startup and the ``hotkey --show`` handler both
    come through here, so a re-issued bind cannot drift from the first one (spec §6.1). A
    re-issue is a bind like any other and starts by stopping what is running, so the portal
    session it replaces is closed rather than leaked.
    """
    return configure_hotkey(
        config.hotkey,
        service,
        session_type=os.environ.get("XDG_SESSION_TYPE", ""),
        portal_available=_portal_available(),
        notifier=notifier,
    )


def _bind_outcome(
    name: str, backend: object | None, deadline: float
) -> tuple[str, list[str], bool]:
    """Wait, until ``deadline``, for the bind just issued to conclude; report what it produced.

    ``bound`` is the portal's own record of the ids the desktop confirmed, so it is the answer
    the moment it arrives. A backend that never reports ids leaves the list empty and the verdict
    false, and the caller says the bind was re-issued rather than claiming an outcome it cannot
    see -- the alternative would be telling an X11 user that their working grabs do not exist.

    ``deadline`` is the caller's own budget, measured from before it handed the request over, so
    a GUI handoff that took most of it leaves the wait the remainder rather than a fresh start: a
    bind that is still waiting on a person is answered as "nothing bound yet", which is what the
    manual instructions are for.
    """
    while True:
        bound = sorted(getattr(backend, "bound", []) or [])
        if bound:
            return name, bound, False
        if _unbound_reported:
            return name, [], True
        if time.monotonic() >= deadline:
            return name, [], False
        time.sleep(REBIND_POLL_INTERVAL)


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
    global _backend, _unbound_reported
    # A second call in one process (a reconfiguration, a rebind, or a second run()) must not
    # leak the backend it replaces: leaving it running would hold a second portal session's grab.
    stop_hotkey()
    _unbound_reported = False  # this bind's outcome, not the one it replaces
    name = select_backend(
        config, session_type=session_type, portal_available=portal_available
    )
    backend = make_backend(name, config)
    if backend is None:
        _bind_concluded_unbound(notifier)
        return name
    bridge = _HotkeyBridge()
    bridge.action.connect(_hotkey_action(service, config, notifier))
    bridge.unbound.connect(lambda: _bind_concluded_unbound(notifier))
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


def _bind_concluded_unbound(notifier: object) -> None:
    """A bind concluded with nothing bound: remember it, and put the manual steps up.

    Reached on the GUI thread -- at once for a ``"none"`` backend, and as a queued call from the
    backend that reports it otherwise -- which is why the record it keeps can be read by
    ``hotkey --show`` as "the bind in place bound nothing".
    """
    global _unbound_reported
    _unbound_reported = True
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


def _load_config() -> Config | None:
    """The config the app runs with, or ``None`` after reporting why there is none.

    ``Config.load`` is deliberately strict: a file that is not TOML, that cannot be read, or
    that names a value its schema rejects (an unknown phonemizer, hotkey backend or log level,
    a field of the wrong type) raises rather than falling back silently, and ``utter doctor``
    prints the same failure as one of its lines. The tray is the one caller with no message
    boundary above it -- ``cli.main`` catches this class and prints it -- so it answers the way
    the missing-model path does: the reason, a pointer at ``doctor``, and a non-zero status,
    which is all a process started from a desktop file and given no terminal can do.
    """
    try:
        return Config.load().merged_with_env()
    except (OSError, ValueError, TypeError) as exc:
        print(f"utter: {exc}", file=sys.stderr)
        print(
            "utter: fix config.toml or the UTTER_* overrides, or run `utter doctor`",
            file=sys.stderr,
        )
        return None


def _setup_logging(level: str) -> None:
    """Install file logging, reporting an unwritable log directory instead of raising.

    A state directory this process cannot write is a diagnostic the app can live without: the
    log is how a support report is read, not how the app speaks. The failure is printed -- the
    only channel left -- and startup continues, with the stdlib's fallback handler still putting
    warnings on stderr.
    """
    try:
        setup_logging(level)
    except OSError as exc:
        print(f"utter: cannot write {paths.log_path()}: {exc}", file=sys.stderr)


def _start_server(server: ControlServer) -> int | None:
    """Serve the control socket, or the exit status to return instead of running the tray.

    ``ControlServer.start`` raises ``RuntimeError`` both when another instance owns the socket --
    the race :func:`delegate_if_running` can lose -- and when the path cannot be served at all:
    a runtime directory that cannot be written, a path something else already holds. Only the
    first is a delegation, and the probe, not the exception's own text, is what tells them apart:
    an instance that is really there answers ``show``. The second case used to return 0, which
    exits successfully with no tray and nothing said -- the one outcome a user cannot act on.
    ``None`` means the socket is served and the tray can run.
    """
    try:
        server.start()
    except RuntimeError as exc:
        if delegate_if_running():
            logger.info("delegating to the instance that owns the socket: %s", exc)
            return 0
        # The message already names the path it could not use; it is the failure that has to
        # reach a terminal, since a tray that never started has no balloon to show it on.
        print(f"utter: {exc}", file=sys.stderr)
        return 1
    return None


def run(argv: list[str] | None = None) -> int:
    """Start the tray application and return the process exit status.

    Zero when this process delegated to a live instance. Otherwise the Qt event loop runs
    until the tray's Quit is chosen (or the session ends), and every thread and socket this
    function created is released before it returns.

    The two things that must hold before Qt exists -- that there is a config to run with and a
    model to speak through -- are checked first, so a broken install is one line on stderr and a
    non-zero status rather than a Qt that cannot start being what reports it.
    """
    if delegate_if_running():
        logger.info("another utter instance owns %s; asked it to show", paths.socket_path())
        return 0

    config = _load_config()
    if config is None:
        return 1
    _setup_logging(config.general.log_level)

    # The model is verified before Qt is built at all: a broken or absent pack is a one-line
    # message and an exit status, whether or not this machine has a display server a tray could
    # be built on -- and a Qt that cannot start must not be the thing that reports it.
    try:
        store = ModelStore(
            paths.models_dir(),
            {
                MODEL_NAME: config.install.model_sha256,
                VOICES_NAME: config.install.voices_sha256,
            },
        )
        store.verify()
        # The tray lists the voices as it is built; an unreadable pack (possible when no
        # digest was recorded) must be this message, not a traceback out of that build.
        store.voice_names()
    except ModelError as exc:
        print(f"utter: {exc}", file=sys.stderr)
        print("utter: run the installer, or `utter doctor`, to install the model", file=sys.stderr)
        return 1

    application = QApplication(argv if argv is not None else [])
    # Nothing this app shows is a window in the quit-on-close sense: the overlay hides and
    # shows while the tray keeps running.
    application.setQuitOnLastWindowClosed(False)

    # Built before the player, so the player's writer thread has somewhere to deliver a
    # device failure: ``_audio_error`` relays it through this bridge, and the tray gets it as
    # a queued balloon once ``_connect_engine`` has wired the error signal up.
    bridge = build_state_bridge()

    player = Player(on_error=_audio_error(bridge))  # constructing it opens nothing
    try:
        player.start()
    except Exception as exc:  # sounddevice's own error type, and anything like it
        # shutdown() is safe on a half-started player: it closes a stream that opened
        # before the failure and joins nothing that was never started.
        player.shutdown()
        print(f"utter: cannot open the audio output: {exc}", file=sys.stderr)
        return 1

    service = SynthService(
        model=store,
        player=player,
        speech=config.speech,
        phonemizer=make_phonemizer(config.speech.phonemizer),
        # Both the engine's callbacks and the player's are emitted off the GUI thread, so all
        # of them go through the bridge: the queued delivery is what keeps a widget (and a
        # balloon) on the GUI thread.
        on_state=bridge.changed.emit,
        on_error=bridge.error.emit,
    )

    tray = Tray(service, config, on_quit=application.quit)
    overlay = Overlay(service, config)
    _connect_engine(bridge, config, tray, overlay)

    # The key is the intent and the desktop file its materialisation, so a file left over from
    # a previous setting (or a config edited by hand) is brought in line before the tray shows.
    # At startup is also when a failure can be reported on a tray that exists.
    reconcile_autostart(config, tray)

    server = ControlServer(
        paths.socket_path(), served_handlers(service, config, notifier=tray)
    )
    status = _start_server(server)
    if status is not None:
        service.shutdown()
        player.shutdown()
        return status

    tray.show()
    try:
        bind_hotkeys(service, config, tray)
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
