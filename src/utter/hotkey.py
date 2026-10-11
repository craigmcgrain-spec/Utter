"""Global hotkey backends: the XDG portal on Wayland and ``XGrabKey`` on X11 (spec §6).

A key press anywhere on the desktop becomes one of three actions -- ``speak``, ``stop`` or
``pause`` -- and reaches the app through the ``on_action`` callback. Which backend to use
is the caller's decision, made by :func:`select_backend` from the session type and the
portal's availability, then built by :func:`make_backend`.

Importing this module touches neither the session bus nor an X display, and neither does
constructing a backend: the first bus call or grab happens in ``start()``. Every failure
is logged and reported through a return value or a flag rather than raised at the caller,
so a missing portal, a declined binding or a dead bus leaves the tray and the CLI working.
"""

from __future__ import annotations

import logging
import select
import threading
from collections.abc import Callable
from itertools import count
from typing import Any, Protocol

from jeepney import (
    DBusAddress,
    DBusErrorResponse,
    HeaderFields,
    MatchRule,
    MessageType,
    new_method_call,
)
from Xlib import X, XK

from .config import Hotkey

logger = logging.getLogger(__name__)

APP_ID = "utter"
"""The portal app-id. It must match the basename of an installed ``.desktop`` file."""

_BUS = "org.freedesktop.DBus"
_BUS_PATH = "/org/freedesktop/DBus"
_PORTAL = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_GLOBAL_SHORTCUTS = "org.freedesktop.portal.GlobalShortcuts"
_REGISTRY_PATH = "/org/freedesktop/portal/desktop"
"""The host portal registry is exported on the portal's own object path.

Measured on this machine (xdg-desktop-portal 1.22.1, and recorded in the design document):
``Registry`` is answered here, while the upstream-documented
``/org/freedesktop/host/portal/Registry`` does not exist -- ``busctl`` answers ``Object does not
exist at path`` -- and the call that does reach the implementation fails only on the app-id
(``App info not found for 'utter'``) until ``install.sh`` ships the ``.desktop`` file.
"""
_REGISTRY_INTERFACE = "org.freedesktop.host.portal.Registry"
_SESSION_INTERFACE = "org.freedesktop.portal.Session"
_REQUEST_INTERFACE = "org.freedesktop.portal.Request"
_REQUEST_PREFIX = "/org/freedesktop/portal/desktop/request"
_SESSION_TOKEN = "utter_session"

_MANUAL_INSTRUCTIONS = (
    "no global hotkey is active; either grant the portal dialog or bind "
    "'utter speak' yourself under Settings -> Keyboard -> Custom Shortcuts"
)

_CALL_TIMEOUT = 10.0
"""Seconds a plain bus method call may take before it counts as a broken bus."""

_REQUEST_TIMEOUT = 300.0
"""Seconds a portal request the user answers in a dialog may take -- bounded on purpose."""

_POLL_TIMEOUT = 0.25
"""Seconds the signal loops block for before re-checking ``stop()``."""

_STOP_TIMEOUT = 2.0
"""Seconds ``stop()`` waits for its worker thread before giving up on it."""


class PortalError(RuntimeError):
    """A portal conversation failed: a D-Bus error, a timeout or a malformed reply."""


class PortalRefused(PortalError):
    """The desktop answered a request with a non-zero (refused or cancelled) response."""


class HotkeyBackend(Protocol):
    """What Task 13's app wiring needs from a hotkey backend (spec §6)."""

    def start(self, on_action: Callable[[str], None]) -> None:
        """Begin delivering ``"speak"``, ``"stop"`` and ``"pause"`` to ``on_action``."""

    def stop(self) -> None:
        """Release every grab and stop delivering actions; safe to call twice."""


def _configured_shortcuts(config: Hotkey) -> list[tuple[str, str]]:
    """The three ``(action id, trigger)`` pairs a backend binds, in dispatch order.

    The triggers are passed through verbatim: the desktop, not Utter, interprets them.
    """
    return [
        ("speak", config.speak),
        ("stop", config.stop),
        ("pause", config.pause),
    ]


_DESCRIPTION = {
    "speak": "Speak selected text",
    "stop": "Stop speaking",
    "pause": "Pause or resume",
}


def _invoke(on_action: Callable[[str], None], action: str) -> None:
    """Deliver one action, keeping a buggy callback from killing the backend thread."""
    try:
        on_action(action)
    except Exception:
        logger.exception("the %s hotkey action failed", action)


def select_backend(config: Hotkey, *, session_type: str, portal_available: bool) -> str:
    """Choose the backend name for ``config``: ``"portal"``, ``"x11"`` or ``"none"``.

    Only ``auto`` chooses: the portal on Wayland when it is available, ``XGrabKey`` on
    X11, and nothing elsewhere. An explicitly configured backend is honoured even when it
    looks unavailable -- the caller, not the selector, decides what to do about that.
    """
    if config.backend != "auto":
        return config.backend
    session = session_type.lower()
    if session == "wayland":
        return "portal" if portal_available else "none"
    if session == "x11":
        return "x11"
    return "none"


def make_backend(name: str, config: Hotkey) -> HotkeyBackend | None:
    """Build the named backend, or ``None`` for ``"none"`` and any unknown name."""
    if name == "portal":
        return PortalHotkey(config)
    if name == "x11":
        return X11Hotkey(config)
    return None


# --- the session bus -------------------------------------------------------------------

_bus_lock = threading.Lock()
_bus: Any = None
"""The process's one session-bus connection, or ``None`` before the first call."""

_subscribed: list[MatchRule] = []
"""Every match rule added on :data:`_bus`, so each ``AddMatch`` has a paired ``RemoveMatch``."""


def _connection():
    """The session-bus connection, opened on first use and then shared.

    One connection serves the whole process: portal request/response pairs are matched per
    connection, and the app-id registration must happen on the same connection the hotkey
    session later uses.
    """
    global _bus
    with _bus_lock:
        if _bus is None:
            from jeepney.io.blocking import open_dbus_connection

            _bus = open_dbus_connection(bus="SESSION")
        return _bus


def _bus_connection():
    """The session-bus connection, or :class:`PortalError` when there is not one.

    Opening the connection is as much a bus failure as sending on it, so it is translated the
    same way: nothing that can be missing from the machine reaches a caller as a bare
    ``OSError`` from the seam.
    """
    try:
        return _connection()
    except OSError as exc:
        raise PortalError(f"the session bus is unavailable: {exc}") from exc


def _drop_connection() -> None:
    """Forget the cached connection so the next call reconnects.

    Closing the socket also drops every match rule the daemon held for it, so no
    ``RemoveMatch`` is sent here; the tracking list is cleared to keep the pairing honest.
    """
    global _bus
    with _bus_lock:
        connection, _bus = _bus, None
        _subscribed.clear()
    if connection is not None:
        try:
            connection.close()
        except OSError:
            pass


def _body(reply):
    """Unwrap a method reply, turning a D-Bus error reply into a :class:`PortalError`.

    Translating at the seam means callers only ever have to know one failure type;
    ``DBusErrorResponse`` itself would slip past a caller's ``except PortalError``.
    """
    if reply.header.message_type == MessageType.error:
        error = DBusErrorResponse(reply)
        detail = f": {error.data}" if error.data else ""
        raise PortalError(f"{error.name}{detail}") from error
    return reply.body


def _is_variant(value: object) -> bool:
    """Whether ``value`` is a variant as jeepney's parser delivers it: ``(signature, value)``."""
    return isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str)


def _unwrapped(results) -> dict:
    """Unwrap one level of the variants a portal ``a{sv}`` reply arrives with.

    Every value in an ``a{sv}`` is a variant, which reaches us as the ``(signature, value)``
    pair jeepney parses it into -- ``{"session_handle": ("s", path)}``. Reading the pairs as
    the values themselves cannot work, so the seam unwraps them once and every caller of
    :func:`_bus_send` sees plain results.
    """
    return {key: value[1] if _is_variant(value) else value for key, value in results.items()}


def _request_path(connection, token: str) -> str:
    """The object path the portal will emit this request's ``Response`` on.

    The portal derives it from the caller's unique name and the ``handle_token``, which is
    why the token is generated before the call: subscribing first is what makes the reply
    unmissable.
    """
    sender = connection.unique_name.lstrip(":").replace(".", "_")
    return f"{_REQUEST_PREFIX}/{sender}/{token}"


def _subscribe(rule: MatchRule) -> None:
    """Ask the bus to deliver messages matching ``rule`` to this connection.

    The rule is recorded only once the daemon has accepted it, so :func:`_unsubscribe`
    never tries to remove a rule the bus never held, and a failed ``AddMatch`` leaves
    nothing behind.
    """
    _bus_send(_BUS, _BUS_PATH, _BUS, "AddMatch", "s", rule.serialise())
    with _bus_lock:
        _subscribed.append(rule)


def _unsubscribe(rule: MatchRule) -> None:
    """Remove a rule :func:`_subscribe` added; one that was never added is a no-op."""
    with _bus_lock:
        if rule not in _subscribed:
            return
        _subscribed.remove(rule)
    try:
        _bus_send(_BUS, _BUS_PATH, _BUS, "RemoveMatch", "s", rule.serialise())
    except Exception:
        logger.debug("could not remove the match rule %s", rule.serialise(), exc_info=True)


def _bus_send(
    destination: str,
    path: str,
    interface: str,
    method: str,
    signature: str | None = None,
    *args: object,
):
    """Issue one D-Bus method call and return its result.

    This is the module's only jeepney call site: registration, session creation, binding,
    session closing and every signal subscription go through it, so a test can replace it to
    simulate a missing bus, a refused request or a declined binding without touching a real
    bus. ``signature`` is the D-Bus signature of ``args``, which is what ``new_method_call``
    needs; each call site names its own, and ``None`` means the empty signature (an absent
    signature field, the way jeepney sends a method with no arguments).

    Every *variant* in an ``a{sv}`` argument must be the ``(signature, value)`` pair the wire
    format uses, and a portal *request* -- a call whose options carry a ``handle_token`` -- is
    answered by a ``Response`` signal rather than a reply body, so this subscribes first, calls,
    then waits and returns that signal's ``results`` with their own variants unwrapped (see
    :func:`_unwrapped`). Every other call returns the method reply body.

    Any failure raises :class:`PortalError`: a D-Bus error reply, a refusal
    (:class:`PortalRefused`), an unanswered request, or a session bus that is not there; an
    ``OSError`` also drops the cached connection so a later call can reconnect.
    """
    connection = _bus_connection()
    address = DBusAddress(path, bus_name=destination, interface=interface)
    options = args[-1] if args and isinstance(args[-1], dict) else None
    # ``handle_token`` is a variant like every other vardict value, so it must be unwrapped
    # before it can become the last element of the request's object path.
    token = _unwrapped(options).get("handle_token") if options else None
    try:
        if token is None:
            call = new_method_call(address, method, signature, args)
            return _body(connection.send_and_get_reply(call, timeout=_CALL_TIMEOUT))
        handle = _request_path(connection, token)
        rule = MatchRule(type="signal", path=handle, interface=_REQUEST_INTERFACE, member="Response")
        _subscribe(rule)
        try:
            with connection.filter(rule) as responses:
                call = new_method_call(address, method, signature, args)
                _body(connection.send_and_get_reply(call, timeout=_CALL_TIMEOUT))
                reply = connection.recv_until_filtered(responses, timeout=_REQUEST_TIMEOUT)
        finally:
            # Each request needs its own path rule; without this the daemon accumulates one
            # pair per portal restart.
            _unsubscribe(rule)
    except TimeoutError as exc:
        raise PortalError(f"{interface}.{method} was not answered in time") from exc
    except OSError as exc:
        _drop_connection()
        raise PortalError(f"the session bus is unavailable: {exc}") from exc
    response, results = reply.body
    if response != 0:
        raise PortalRefused(f"{interface}.{method} was refused (response {response})")
    return _unwrapped(results)


def register_with_portal() -> bool:
    """Claim the app-id for this process, returning ``False`` when it cannot be claimed.

    The portal's app-id rule makes this the process's *first* portal call, so it must run
    before Qt or any other portal use. Failure is logged and non-fatal -- the tray and the
    CLI keep working -- which is why this never raises.
    """
    try:
        _bus_send(_PORTAL, _REGISTRY_PATH, _REGISTRY_INTERFACE, "Register", "sa{sv}", APP_ID, {})
    except Exception:
        logger.warning(
            "could not register the app-id %r with the portal; global shortcuts may be "
            "unavailable",
            APP_ID,
            exc_info=True,
        )
        return False
    logger.debug("registered the app-id %r with the portal", APP_ID)
    return True


# --- Wayland: the portal ---------------------------------------------------------------


class PortalHotkey:
    """Wayland global shortcuts through the XDG portal (spec §6.1).

    All bus work runs on a worker thread, so a bind dialog or a slow desktop never blocks
    the GUI thread, and every wait is bounded. A refusal is reported once, in the log and
    through :attr:`declined`, and the app keeps working without a hotkey.
    """

    def __init__(self, config: Hotkey) -> None:
        self._config = config
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tokens = count(1)
        self.session: str | None = None
        """The portal session object path, once the desktop has created one."""

        self.bound: list[str] = []
        """The action ids the desktop actually bound; empty when it bound none."""

        self.declined = False
        """True when no shortcut can be delivered: the desktop refused, bound none, a call
        failed, or the listener could not start. ``bound`` still lists whatever was bound."""

    def shortcuts(self) -> list[tuple[str, str]]:
        """The ``(action id, preferred trigger)`` pairs this backend asks the desktop for."""
        return _configured_shortcuts(self._config)

    def start(self, on_action: Callable[[str], None]) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, args=(on_action,), name="utter-portal-hotkey", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=_STOP_TIMEOUT)
        if thread.is_alive():
            logger.warning(
                "the portal hotkey thread is still waiting on the desktop and will not "
                "block exit"
            )
        else:
            self._thread = None

    def _run(self, on_action: Callable[[str], None]) -> None:
        try:
            self._bind()
            self._listen(on_action)
        except PortalError as exc:
            # A listener that cannot start means no press can ever be delivered, whatever the
            # bind said; a failure ``_bind`` already recorded is not logged a second time.
            if not self.declined:
                self._report_unbound(str(exc))
        except Exception:
            logger.exception("the portal hotkey backend stopped")
        finally:
            # ``stop()`` ends the serve loop, so the desktop's grab is released on this
            # thread -- the connection's owner -- rather than on the caller's.
            self._close_session()

    def _bind(self) -> None:
        """Create a session and bind the three shortcuts, recording whatever happened.

        Every failure -- a D-Bus error reply, a timeout, an unreachable bus, a refusal, a
        malformed reply or an empty bound set -- leaves :attr:`declined` set and
        :attr:`bound` empty, so the caller can notify the user instead of losing the outcome
        in the log; a bug in this module still propagates, so it is not mistaken for a decline.
        """
        try:
            results = _bus_send(
                _PORTAL,
                _PORTAL_PATH,
                _GLOBAL_SHORTCUTS,
                "CreateSession",
                "a{sv}",
                {
                    "handle_token": ("s", self._token()),
                    "session_handle_token": ("s", _SESSION_TOKEN),
                },
            )
            session = results.get("session_handle")
            if not session:
                raise PortalError("the portal created no session handle")
            self.session = session
            entries = [
                (
                    action,
                    {
                        "description": ("s", _DESCRIPTION[action]),
                        "preferred_trigger": ("s", trigger),
                    },
                )
                for action, trigger in self.shortcuts()
            ]
            results = _bus_send(
                _PORTAL,
                _PORTAL_PATH,
                _GLOBAL_SHORTCUTS,
                "BindShortcuts",
                "oa(sa{sv})sa{sv}",
                session,
                entries,
                "",
                {"handle_token": ("s", self._token())},
            )
            # The reply's ``shortcuts`` is what the desktop actually bound -- a subset of
            # what was asked for, and the empty set when the user declined the dialog.
            self.bound = self._bound_ids(results.get("shortcuts", ()))
        except PortalError as exc:
            self._report_unbound(str(exc))
            return
        if not self.bound:
            self._report_unbound("the desktop bound no shortcuts")
            return
        logger.info("bound global shortcuts %s on session %s", ", ".join(self.bound), session)

    @staticmethod
    def _bound_ids(shortcuts) -> list[str]:
        """The ids from a ``shortcuts`` reply field; a shape we cannot read is a portal error."""
        try:
            return sorted(entry[0] for entry in shortcuts)
        except (TypeError, ValueError) as exc:
            raise PortalError(f"the portal returned unreadable shortcuts: {shortcuts!r}") from exc

    def _close_session(self) -> None:
        """Release the desktop's grab; a session the portal never created needs no closing.

        The signature is the empty one -- ``None`` omits the field, which is how jeepney sends
        a method with no arguments. A worker still parked in a request wait cannot be
        interrupted, so if the desktop is mid-dialog the close happens when that wait returns
        (or when the process exits) -- the only residue ``stop()`` cannot bound.
        """
        if self.session is None:
            return
        session, self.session = self.session, None
        try:
            _bus_send(_PORTAL, session, _SESSION_INTERFACE, "Close", None)
        except Exception:
            logger.warning("could not close the portal session %s", session, exc_info=True)

    def _report_unbound(self, why: str) -> None:
        self.declined = True
        self.bound = []
        logger.warning("the global hotkeys are not bound (%s); %s", why, _MANUAL_INSTRUCTIONS)

    def _listen(self, on_action: Callable[[str], None]) -> None:
        """Subscribe to activations and portal restarts, then serve them until ``stop()``."""
        connection = _bus_connection()
        activated_rule = MatchRule(
            type="signal", interface=_GLOBAL_SHORTCUTS, member="Activated"
        )
        owner_rule = MatchRule(
            type="signal", sender=_BUS, interface=_BUS, member="NameOwnerChanged", path=_BUS_PATH
        )
        owner_rule.add_arg_condition(0, _PORTAL)
        try:
            # Both subscribes are inside the try so a failure of the second still removes
            # the first; ``_unsubscribe`` ignores a rule that was never accepted.
            _subscribe(activated_rule)
            _subscribe(owner_rule)
            with (
                connection.filter(activated_rule, bufsize=64) as activated,
                connection.filter(owner_rule, bufsize=8) as owners,
            ):
                self._serve(self._messages(connection, activated, owners), on_action)
        finally:
            # ``stop()`` ends the serve loop, so the rules are removed on the connection's
            # own thread; a connection that died in the meantime drops them with its socket.
            _unsubscribe(activated_rule)
            _unsubscribe(owner_rule)

    def _messages(self, connection, activated, owners):
        """Yield each signal the bus delivers, until ``stop()`` or a bus that has gone away.

        The receive is bounded by the poll timeout, which is what lets ``stop()`` be noticed
        without closing the connection from another thread.
        """
        while not self._stop.is_set():
            try:
                connection.recv_messages(timeout=_POLL_TIMEOUT)
            except TimeoutError:
                continue
            except OSError:
                logger.warning("the session bus went away; global hotkeys are offline")
                return
            while activated:
                yield activated.popleft()
            while owners:
                yield owners.popleft()

    def _serve(self, messages, on_action: Callable[[str], None]) -> None:
        """Route each signal: activations to the callback, a new portal owner to a rebind."""
        for message in messages:
            member = message.header.fields.get(HeaderFields.member)
            if member == "Activated":
                self._dispatch(message, on_action)
            elif member == "NameOwnerChanged" and message.body[2]:
                logger.info("the portal restarted; rebinding the global shortcuts")
                register_with_portal()
                self._bind()

    def _dispatch(self, message, on_action: Callable[[str], None]) -> None:
        session, shortcut_id = message.body[0], message.body[1]
        if session != self.session:
            logger.debug("ignoring an activation for the stale session %s", session)
            return
        if shortcut_id not in _DESCRIPTION:
            logger.debug("ignoring the unknown shortcut id %r", shortcut_id)
            return
        _invoke(on_action, shortcut_id)

    def _token(self) -> str:
        """A fresh ``handle_token``: a valid object-path element, unique per request."""
        return f"utter_{next(self._tokens)}"


# --- X11: XGrabKey ---------------------------------------------------------------------

_MODIFIER_MASKS = {
    "SHIFT": X.ShiftMask,
    "LOCK": X.LockMask,
    "CAPS": X.LockMask,
    "CONTROL": X.ControlMask,
    "CTRL": X.ControlMask,
    "ALT": X.Mod1Mask,
    "MOD1": X.Mod1Mask,
    "MOD2": X.Mod2Mask,
    "MOD3": X.Mod3Mask,
    "LOGO": X.Mod4Mask,
    "MOD4": X.Mod4Mask,
    "SUPER": X.Mod4Mask,
    "MOD5": X.Mod5Mask,
}

_LOCK_MASK = X.LockMask | X.Mod2Mask
"""CapsLock and NumLock, which alter ``state``: grabbed in every combination, ignored on press."""

_GRAB_VARIANTS = (0, X.LockMask, X.Mod2Mask, _LOCK_MASK)


def _parse_trigger(trigger: str) -> tuple[int, str]:
    """Split an ``xkbcommon``-style ``MOD+MOD+key`` trigger into a modifier mask and a key."""
    parts = [part.strip() for part in trigger.split("+")]
    if not parts or any(not part for part in parts):
        raise ValueError(f"malformed hotkey trigger {trigger!r}")
    modifiers = 0
    for name in parts[:-1]:
        try:
            modifiers |= _MODIFIER_MASKS[name.upper()]
        except KeyError:
            raise ValueError(f"unknown hotkey modifier {name!r} in {trigger!r}") from None
    return modifiers, parts[-1]


class X11Hotkey:
    """Root-window ``XGrabKey`` grabs, one per trigger, on a dedicated thread (spec §6.2).

    The grab survives keyboard-layout changes by re-grabbing on ``MappingNotify``, and
    needs an X display -- nothing happens until :meth:`start` runs. ``auto`` never selects
    this backend on Wayland; that rule lives in :func:`select_backend`.
    """

    def __init__(self, config: Hotkey) -> None:
        self._config = config
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, on_action: Callable[[str], None]) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, args=(on_action,), name="utter-x11-hotkey", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=_STOP_TIMEOUT)
        if thread.is_alive():
            logger.warning("the X11 hotkey thread did not stop within %.1fs", _STOP_TIMEOUT)
        else:
            self._thread = None

    def _run(self, on_action: Callable[[str], None]) -> None:
        from Xlib.display import Display

        try:
            display = Display()
        except Exception:
            logger.exception("could not open an X display; global hotkeys are offline")
            return
        try:
            self._loop(display, on_action)
        except Exception:
            logger.exception("the X11 hotkey backend stopped")
        finally:
            try:
                display.close()
            except Exception:
                logger.debug("closing the X display failed", exc_info=True)

    def _loop(self, display, on_action: Callable[[str], None]) -> None:
        root = display.screen().root
        bindings = self._grab(root, display)
        try:
            while not self._stop.is_set():
                if not display.pending_events():
                    if not select.select([display.fileno()], [], [], _POLL_TIMEOUT)[0]:
                        continue
                event = display.next_event()
                if event.type == X.KeyPress:
                    action = bindings.get((event.detail, event.state & ~_LOCK_MASK))
                    if action is not None:
                        _invoke(on_action, action)
                elif event.type == X.MappingNotify:
                    self._ungrab(root, bindings)
                    display.refresh_keyboard_mapping(event)
                    bindings = self._grab(root, display)
        finally:
            self._ungrab(root, bindings)

    def _grab(self, root, display) -> dict[tuple[int, int], str]:
        """Grab every configured trigger and return its ``(keycode, modifiers)`` index."""
        bindings: dict[tuple[int, int], str] = {}
        for action, trigger in _configured_shortcuts(self._config):
            try:
                modifiers, key_name = _parse_trigger(trigger)
            except ValueError as exc:
                logger.warning("ignoring the %s hotkey: %s", action, exc)
                continue
            keysym = XK.string_to_keysym(key_name)
            keycode = display.keysym_to_keycode(keysym) if keysym else 0
            if not keycode:
                logger.warning(
                    "ignoring the %s hotkey %r: %r is not a key on this keyboard",
                    action,
                    trigger,
                    key_name,
                )
                continue
            if (keycode, modifiers) in bindings:
                logger.warning(
                    "the hotkey %r is already bound to %s; ignoring it for %s",
                    trigger,
                    bindings[(keycode, modifiers)],
                    action,
                )
                continue
            for variant in _GRAB_VARIANTS:
                root.grab_key(
                    keycode,
                    modifiers | variant,
                    True,
                    X.GrabModeAsync,
                    X.GrabModeAsync,
                    onerror=self._grab_error(action, trigger),
                )
            bindings[(keycode, modifiers)] = action
        return bindings

    def _ungrab(self, root, bindings: dict[tuple[int, int], str]) -> None:
        """Release every grab; a display that has already gone away is not an error."""
        for keycode, modifiers in bindings:
            for variant in _GRAB_VARIANTS:
                try:
                    root.ungrab_key(keycode, modifiers | variant, onerror=lambda *_: None)
                except Exception:
                    logger.debug("could not release the grab for keycode %s", keycode, exc_info=True)
                    return

    def _grab_error(self, action: str, trigger: str):
        def on_error(*_args: object) -> None:
            logger.warning(
                "could not grab %r for %s; another program probably owns it", trigger, action
            )

        return on_error
