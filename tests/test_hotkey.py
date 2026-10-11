import threading
from collections import deque, namedtuple

import pytest
from jeepney import (
    DBusAddress,
    HeaderFields,
    Message,
    new_error,
    new_method_return,
    new_signal,
)

import utter.hotkey as hotkey
from utter.hotkey import PortalHotkey, X11Hotkey, make_backend, select_backend


def test_auto_picks_portal_on_wayland(config):
    assert select_backend(config.hotkey, session_type="wayland", portal_available=True) == "portal"

def test_auto_falls_back_to_none_without_a_portal(config):
    assert select_backend(config.hotkey, session_type="wayland", portal_available=False) == "none"

def test_auto_picks_x11_on_x11(config):
    assert select_backend(config.hotkey, session_type="x11", portal_available=True) == "x11"

def test_explicit_none_is_respected(config):
    config.hotkey.backend = "none"
    assert select_backend(config.hotkey, session_type="wayland", portal_available=True) == "none"

def test_make_backend_returns_nothing_for_none(config):
    assert make_backend("none", config.hotkey) is None

def test_trigger_strings_are_passed_through_unchanged(config):
    backend = PortalHotkey(config.hotkey)
    assert backend.shortcuts() == [
        ("speak", "LOGO+ALT+s"), ("stop", "LOGO+ALT+x"), ("pause", "LOGO+ALT+p"),
    ]

def test_make_backend_builds_the_named_backend(config):
    assert isinstance(make_backend("portal", config.hotkey), PortalHotkey)
    assert isinstance(make_backend("x11", config.hotkey), X11Hotkey)

def test_explicit_portal_is_respected_even_when_it_looks_unavailable(config):
    # The caller decides what to do about an unavailable portal; the selector must not
    # silently substitute another backend for an explicitly requested one.
    config.hotkey.backend = "portal"
    assert select_backend(config.hotkey, session_type="x11", portal_available=False) == "portal"
    config.hotkey.backend = "x11"
    assert select_backend(config.hotkey, session_type="wayland", portal_available=True) == "x11"

def test_registering_with_a_broken_bus_reports_failure_instead_of_raising(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("no session bus")
    monkeypatch.setattr(hotkey, "_bus_send", boom)
    assert hotkey.register_with_portal() is False


# --- the portal conversation, through the _bus_send seam --------------------------------

_PORTAL = "org.freedesktop.portal.Desktop"
_GLOBAL_SHORTCUTS = "org.freedesktop.portal.GlobalShortcuts"
_BUS = "org.freedesktop.DBus"
_BUS_PATH = "/org/freedesktop/DBus"
_SESSION = "/org/freedesktop/portal/desktop/session/1/utter"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_REGISTRY_PATH = "/org/freedesktop/portal/desktop"
"""Measured on this machine: the host portal registry is on the portal's own object path."""
_REGISTRY_INTERFACE = "org.freedesktop.host.portal.Registry"
_SESSION_INTERFACE = "org.freedesktop.portal.Session"
_REQUEST_INTERFACE = "org.freedesktop.portal.Request"


@pytest.fixture(autouse=True)
def _clean_hotkey_state():
    """The match-rule registry is module state; every test starts and ends with it empty."""
    hotkey._subscribed.clear()
    yield
    hotkey._subscribed.clear()


Call = namedtuple("Call", "destination path interface method signature args")


def _s(text):
    """A ``(signature, value)`` pair: what one ``s`` value inside an ``a{sv}`` must be."""
    return ("s", text)


def _recorder(answers):
    """A ``_bus_send`` stand-in: records each full call and answers from ``answers``.

    The destination, object path, interface and signature are recorded too, so a wrong
    constant in the module fails the suite instead of hiding behind the patched seam. Each
    answer is the unwrapped results dict that ``_bus_send`` hands back.
    """
    calls = []

    def send(destination, path, interface, method, signature=None, *args):
        calls.append(Call(destination, path, interface, method, signature, args))
        answer = answers.get(method, ())
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return send, calls


def _signal(member, signature, body, *, interface=_GLOBAL_SHORTCUTS, path=_PORTAL_PATH,
            sender=_PORTAL):
    """A real jeepney signal message, built without a bus."""
    address = DBusAddress(path, bus_name=sender, interface=interface)
    return new_signal(address, member, signature, body)


class _StubConnection:
    """The smallest connection the real ``_bus_send`` needs: it records what was sent.

    ``responses`` maps a method name to the *raw* results its ``Response`` signal carries --
    variant pairs and all, exactly as a real bus delivers them.
    """

    unique_name = ":1.42"

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.sent = []
        self.rule = None
        self._member = None

    def send_and_get_reply(self, message, timeout=None):
        self.sent.append(message)
        self._member = message.header.fields.get(HeaderFields.member)
        return new_method_return(message, "", ())

    def filter(self, rule, bufsize=1):
        self.rule = rule
        return _StubFilter(deque(maxlen=bufsize))

    def recv_until_filtered(self, queue, timeout=None):
        return _signal(
            "Response", "ua{sv}", (0, self.responses.get(self._member, {})),
            interface=_REQUEST_INTERFACE,
        )

    def close(self):
        pass


class _StubFilter:
    def __init__(self, queue):
        self.queue = queue

    def __enter__(self):
        return self.queue

    def __exit__(self, *exc):
        return False


class _ErrorStubConnection(_StubConnection):
    """A connection whose reply to one method is a D-Bus error."""

    def __init__(self, failing, error_name="org.freedesktop.portal.Error.Failed"):
        super().__init__()
        self.failing = failing
        self.error_name = error_name

    def send_and_get_reply(self, message, timeout=None):
        self.sent.append(message)
        if message.header.fields.get(HeaderFields.member) == self.failing:
            return new_error(message, self.error_name, "s", ("the desktop said no",))
        return new_method_return(message, "", ())


class _DeadStubConnection(_StubConnection):
    """A connection whose session bus is not there at all."""

    def send_and_get_reply(self, message, timeout=None):
        raise OSError("no session bus")


def _round_trip(message):
    """Serialise a message and parse it back: a body that cannot go on the wire fails here."""
    return Message.from_buffer(message.serialise(serial=1))


def _field(message, field):
    return message.header.fields.get(field)


def test_bus_send_builds_and_serialises_the_wire_message(monkeypatch):
    """The real seam, not a patch of it: no other test builds or serialises a message."""
    connection = _StubConnection({"CreateSession": {"session_handle": _s(_SESSION)}})
    monkeypatch.setattr(hotkey, "_connection", lambda: connection)

    assert hotkey._bus_send(
        _PORTAL, _REGISTRY_PATH, _REGISTRY_INTERFACE, "Register", "sa{sv}", hotkey.APP_ID, {},
    ) == ()
    results = hotkey._bus_send(
        _PORTAL, _PORTAL_PATH, _GLOBAL_SHORTCUTS, "CreateSession", "a{sv}",
        {"handle_token": _s("utter_1"), "session_handle_token": _s("utter_session")},
    )

    assert results == {"session_handle": _SESSION}  # the variant pair was unwrapped
    register, add_match, create, remove_match = connection.sent
    assert (
        _field(register, HeaderFields.path),
        _field(register, HeaderFields.destination),
        _field(register, HeaderFields.interface),
        _field(register, HeaderFields.member),
        _field(register, HeaderFields.signature),
        register.body,
    ) == (
        _REGISTRY_PATH, _PORTAL, _REGISTRY_INTERFACE, "Register",
        "sa{sv}", (hotkey.APP_ID, {}),
    )
    assert create.body == (
        {"handle_token": _s("utter_1"), "session_handle_token": _s("utter_session")},
    )
    for message, member, signature in (
        (add_match, "AddMatch", "s"),
        (create, "CreateSession", "a{sv}"),
        (remove_match, "RemoveMatch", "s"),
    ):
        assert _field(message, HeaderFields.member) == member
        assert _field(message, HeaderFields.signature) == signature
    assert (
        _field(create, HeaderFields.destination),
        _field(create, HeaderFields.path),
        _field(create, HeaderFields.interface),
    ) == (_PORTAL, _PORTAL_PATH, _GLOBAL_SHORTCUTS)
    for message in connection.sent:  # a malformed variant cannot survive this
        assert _round_trip(message).body == message.body
    # the match rule pins the predicted request path, built from the *unwrapped* token
    assert connection.rule.header_fields["path"] == (
        "/org/freedesktop/portal/desktop/request/1_42/utter_1"
    )
    assert hotkey._subscribed == []  # the request rule was removed in the finally


def test_bus_send_serialises_the_bind_request(monkeypatch):
    connection = _StubConnection({"BindShortcuts": {"shortcuts": ("a(sa{sv})", [("speak", {})])}})
    monkeypatch.setattr(hotkey, "_connection", lambda: connection)
    entries = [
        ("speak", {"description": _s("Speak selected text"), "preferred_trigger": _s("LOGO+ALT+s")}),
    ]

    results = hotkey._bus_send(
        _PORTAL, _PORTAL_PATH, _GLOBAL_SHORTCUTS, "BindShortcuts", "oa(sa{sv})sa{sv}",
        _SESSION, entries, "", {"handle_token": _s("utter_2")},
    )

    assert results == {"shortcuts": [("speak", {})]}
    bind = connection.sent[1]  # 0 is AddMatch, 2 is RemoveMatch
    assert _field(bind, HeaderFields.member) == "BindShortcuts"
    assert _field(bind, HeaderFields.signature) == "oa(sa{sv})sa{sv}"
    assert bind.body == (_SESSION, entries, "", {"handle_token": _s("utter_2")})
    assert _round_trip(bind).body == bind.body
    assert hotkey._subscribed == []


def test_bus_send_serialises_a_no_argument_call_without_a_signature_field(monkeypatch):
    connection = _StubConnection()
    monkeypatch.setattr(hotkey, "_connection", lambda: connection)

    assert hotkey._bus_send(_PORTAL, _SESSION, _SESSION_INTERFACE, "Close", None) == ()

    (close,) = connection.sent
    assert _field(close, HeaderFields.member) == "Close"
    assert HeaderFields.signature not in close.header.fields  # the empty signature
    assert close.body == ()


def test_portal_bind_creates_a_session_and_binds_the_three_triggers(config, monkeypatch):
    send, calls = _recorder({
        "CreateSession": {"session_handle": _SESSION},
        "BindShortcuts": {"shortcuts": [("speak", {}), ("stop", {}), ("pause", {})]},
    })
    monkeypatch.setattr(hotkey, "_bus_send", send)

    backend = hotkey.PortalHotkey(config.hotkey)
    backend._bind()

    assert backend.session == _SESSION
    assert backend.bound == ["pause", "speak", "stop"]
    assert backend.declined is False
    assert [call.method for call in calls] == ["CreateSession", "BindShortcuts"]
    assert [(call.destination, call.path, call.interface) for call in calls] == [
        (_PORTAL, _PORTAL_PATH, _GLOBAL_SHORTCUTS),
        (_PORTAL, _PORTAL_PATH, _GLOBAL_SHORTCUTS),
    ]
    assert [call.signature for call in calls] == ["a{sv}", "oa(sa{sv})sa{sv}"]

    options = calls[0].args[0]
    assert options.keys() == {"handle_token", "session_handle_token"}
    assert all(signature == "s" and value for signature, value in options.values())
    session, entries, parent_window, bind_options = calls[1].args
    assert session == _SESSION
    assert parent_window == ""
    assert options["handle_token"] != bind_options["handle_token"]
    assert bind_options.keys() == {"handle_token"} and bind_options["handle_token"][0] == "s"
    assert [(action, vardict["preferred_trigger"], vardict["description"]) for action, vardict in entries] == [
        ("speak", _s("LOGO+ALT+s"), _s("Speak selected text")),
        ("stop", _s("LOGO+ALT+x"), _s("Stop speaking")),
        ("pause", _s("LOGO+ALT+p"), _s("Pause or resume")),
    ]


@pytest.mark.parametrize("answer", [
    pytest.param({}, id="empty-set"),
    pytest.param(hotkey.PortalRefused("cancelled"), id="refused-response"),
])
def test_portal_declined_bind_is_reported_and_start_does_not_raise(config, monkeypatch, answer):
    send, calls = _recorder({"CreateSession": {"session_handle": _SESSION}, "BindShortcuts": answer})
    monkeypatch.setattr(hotkey, "_bus_send", send)
    monkeypatch.setattr(hotkey.PortalHotkey, "_listen", lambda self, on_action: None)

    backend = hotkey.PortalHotkey(config.hotkey)
    backend.start(lambda action: None)  # the caller must survive a decline and notify instead
    backend._thread.join(timeout=5)

    assert not backend._thread.is_alive()
    assert backend.declined is True  # the outcome is visible, not only logged
    assert backend.bound == []
    assert [call.method for call in calls][0] == "CreateSession"
    backend.stop()
    assert backend._thread is None


@pytest.mark.parametrize("answer", [
    pytest.param({}, id="empty-set"),
    pytest.param(hotkey.PortalRefused("cancelled"), id="refused-response"),
])
def test_a_declined_bind_reports_the_outcome_through_on_unbound(config, monkeypatch, answer):
    """The app cannot ask ``bound`` after ``start()``: the bind concludes later, on the worker.

    So the outcome is pushed through ``on_unbound`` -- once, from the backend's own thread --
    and a bind that succeeds never calls it. That is what lets the tray notify without
    blocking startup on a dialog and without a widget being touched off the GUI thread.
    """
    send, _ = _recorder({"CreateSession": {"session_handle": _SESSION}, "BindShortcuts": answer})
    monkeypatch.setattr(hotkey, "_bus_send", send)
    monkeypatch.setattr(hotkey.PortalHotkey, "_listen", lambda self, on_action: None)

    backend = hotkey.PortalHotkey(config.hotkey)
    seen = []
    backend.start(lambda action: None, on_unbound=lambda: seen.append(threading.current_thread()))
    backend._thread.join(timeout=5)

    assert [thread.name for thread in seen] == ["utter-portal-hotkey"], (
        "the outcome is reported once, from the worker thread start() spawned"
    )
    assert seen[0] is not threading.main_thread()
    backend.stop()


def test_a_successful_bind_never_calls_on_unbound(config, monkeypatch):
    send, _ = _recorder({
        "CreateSession": {"session_handle": _SESSION},
        "BindShortcuts": {"shortcuts": [("speak", {})]},
    })
    monkeypatch.setattr(hotkey, "_bus_send", send)
    monkeypatch.setattr(hotkey.PortalHotkey, "_listen", lambda self, on_action: None)

    backend = hotkey.PortalHotkey(config.hotkey)
    seen = []
    backend.start(lambda action: None, on_unbound=lambda: seen.append(True))
    backend._thread.join(timeout=5)

    assert backend.bound == ["speak"]
    assert seen == [], "a bind that succeeded needs no manual instructions"
    backend.stop()


@pytest.mark.parametrize("answer", [
    pytest.param({}, id="empty-set"),
    pytest.param(hotkey.PortalRefused("cancelled"), id="refused-response"),
])
def test_a_declined_bind_is_visible_through_the_public_attributes(config, monkeypatch, answer):
    send, _ = _recorder({"CreateSession": {"session_handle": _SESSION}, "BindShortcuts": answer})
    monkeypatch.setattr(hotkey, "_bus_send", send)

    backend = hotkey.PortalHotkey(config.hotkey)
    backend._bind()  # the same call the worker makes; it must not raise

    assert backend.declined is True
    assert backend.bound == []
    assert backend.session == _SESSION  # the session existed; the decline is about shortcuts


def test_portal_bind_reads_a_variant_shaped_reply(config, monkeypatch):
    """A real Response carries ``(signature, value)`` variants, as jeepney delivers them."""
    connection = _StubConnection({
        "CreateSession": {"session_handle": _s(_SESSION)},
        "BindShortcuts": {"shortcuts": ("a(sa{sv})", [
            ("speak", {"description": _s("Speak selected text"), "trigger_description": _s("Logo+Alt+S")}),
            ("stop", {"description": _s("Stop speaking"), "trigger_description": _s("Logo+Alt+X")}),
        ])},
    })
    monkeypatch.setattr(hotkey, "_connection", lambda: connection)

    backend = hotkey.PortalHotkey(config.hotkey)
    backend._bind()

    assert backend.session == _SESSION
    assert backend.bound == ["speak", "stop"]
    assert backend.declined is False


@pytest.mark.parametrize("results", [
    pytest.param({}, id="no-shortcuts-key"),
    pytest.param({"shortcuts": ("a(sa{sv})", [])}, id="empty-shortcuts"),
])
def test_portal_bind_reads_a_declined_variant_reply(config, monkeypatch, results):
    connection = _StubConnection({
        "CreateSession": {"session_handle": _s(_SESSION)},
        "BindShortcuts": results,
    })
    monkeypatch.setattr(hotkey, "_connection", lambda: connection)

    backend = hotkey.PortalHotkey(config.hotkey)
    backend._bind()

    assert backend.declined is True
    assert backend.bound == []
    assert backend.session == _SESSION


def test_bus_send_raises_portal_error_for_a_dbus_error_reply(monkeypatch):
    monkeypatch.setattr(hotkey, "_connection", lambda: _ErrorStubConnection("Register"))

    with pytest.raises(hotkey.PortalError) as raised:
        hotkey._bus_send(
            _PORTAL, _REGISTRY_PATH, _REGISTRY_INTERFACE, "Register", "sa{sv}", hotkey.APP_ID, {},
        )

    assert "org.freedesktop.portal.Error.Failed" in str(raised.value)
    assert not isinstance(raised.value, hotkey.PortalRefused)


def test_bus_send_raises_portal_error_when_the_bus_is_unreachable(monkeypatch):
    monkeypatch.setattr(hotkey, "_connection", lambda: _DeadStubConnection())

    with pytest.raises(hotkey.PortalError) as raised:
        hotkey._bus_send(
            _PORTAL, _REGISTRY_PATH, _REGISTRY_INTERFACE, "Register", "sa{sv}", hotkey.APP_ID, {},
        )

    assert not isinstance(raised.value, OSError)


def test_bus_send_raises_portal_error_when_the_bus_cannot_be_opened(monkeypatch):
    def no_bus():
        raise OSError("no session bus")

    monkeypatch.setattr(hotkey, "_connection", no_bus)

    with pytest.raises(hotkey.PortalError) as raised:
        hotkey._bus_send(
            _PORTAL, _REGISTRY_PATH, _REGISTRY_INTERFACE, "Register", "sa{sv}", hotkey.APP_ID, {},
        )

    assert "session bus" in str(raised.value)
    assert not isinstance(raised.value, OSError)


def test_a_bind_that_cannot_open_the_bus_is_visible_and_does_not_raise(config, monkeypatch):
    def no_bus():
        raise OSError("no session bus")

    monkeypatch.setattr(hotkey, "_connection", no_bus)
    backend = hotkey.PortalHotkey(config.hotkey)

    backend._bind()  # the worker's first call; the connection may not even open

    assert backend.declined is True
    assert backend.bound == []
    assert backend.session is None


def test_start_does_not_raise_when_the_bus_cannot_be_opened(config, monkeypatch):
    def no_bus():
        raise OSError("no session bus")

    monkeypatch.setattr(hotkey, "_connection", no_bus)
    backend = hotkey.PortalHotkey(config.hotkey)
    seen = []

    backend.start(seen.append)  # nothing may escape into the caller
    backend._thread.join(timeout=5)

    assert not backend._thread.is_alive()
    assert backend.declined is True
    assert backend.bound == []
    assert backend.session is None
    assert seen == []  # and no action can be delivered
    backend.stop()
    assert backend._thread is None


def test_a_listener_that_cannot_start_marks_the_backend_unavailable(config, monkeypatch):
    """A bind can succeed and the listener still never run; that must not read as success."""
    send, calls = _recorder({
        "CreateSession": {"session_handle": _SESSION},
        "BindShortcuts": {"shortcuts": [("speak", {})]},
    })
    monkeypatch.setattr(hotkey, "_bus_send", send)

    def no_bus():
        raise OSError("no session bus")

    monkeypatch.setattr(hotkey, "_connection", no_bus)
    backend = hotkey.PortalHotkey(config.hotkey)
    seen = []

    backend.start(seen.append)
    backend._thread.join(timeout=5)

    assert not backend._thread.is_alive()
    assert [call.method for call in calls].count("BindShortcuts") == 1  # the bind did work ...
    assert backend.declined is True  # ... but no press can be delivered
    assert backend.bound == []
    assert seen == []
    assert "Close" in [call.method for call in calls]  # the session was still released
    assert backend.session is None
    backend.stop()
    assert backend._thread is None


def test_a_bind_that_hits_a_dbus_error_is_visible_and_does_not_raise(config, monkeypatch):
    monkeypatch.setattr(hotkey, "_connection", lambda: _ErrorStubConnection("CreateSession"))
    backend = hotkey.PortalHotkey(config.hotkey)

    backend._bind()  # the worker's first call; a caller needs an outcome, not a traceback

    assert backend.declined is True
    assert backend.bound == []
    assert backend.session is None
    assert hotkey._subscribed == []


def test_a_bind_that_cannot_reach_the_bus_is_visible_and_does_not_raise(config, monkeypatch):
    monkeypatch.setattr(hotkey, "_connection", lambda: _DeadStubConnection())
    backend = hotkey.PortalHotkey(config.hotkey)

    backend._bind()

    assert backend.declined is True
    assert backend.bound == []
    assert backend.session is None


def test_start_does_not_raise_when_the_bus_is_unreachable(config, monkeypatch):
    monkeypatch.setattr(hotkey, "_connection", lambda: _DeadStubConnection())
    backend = hotkey.PortalHotkey(config.hotkey)

    backend.start(lambda action: None)  # nothing may escape into the caller
    backend._thread.join(timeout=5)

    assert not backend._thread.is_alive()
    assert backend.declined is True
    assert backend.bound == []
    backend.stop()
    assert backend._thread is None


def test_portal_closes_the_session_when_it_stops(config, monkeypatch):
    send, calls = _recorder({
        "CreateSession": {"session_handle": _SESSION},
        "BindShortcuts": {"shortcuts": [("speak", {})]},
    })
    monkeypatch.setattr(hotkey, "_bus_send", send)
    monkeypatch.setattr(hotkey.PortalHotkey, "_listen", lambda self, on_action: None)

    backend = hotkey.PortalHotkey(config.hotkey)
    backend.start(lambda action: None)
    backend._thread.join(timeout=5)
    backend.stop()

    close = [call for call in calls if call.method == "Close"]
    assert len(close) == 1
    assert (
        close[0].destination, close[0].path, close[0].interface, close[0].signature, close[0].args,
    ) == (_PORTAL, _SESSION, _SESSION_INTERFACE, None, ())
    assert backend.session is None


@pytest.mark.parametrize("action", ["speak", "stop"])
def test_portal_serves_activated_shortcuts_to_the_callback(config, monkeypatch, action):
    monkeypatch.setattr(hotkey, "_bus_send", lambda *args, **kwargs: ())
    backend = hotkey.PortalHotkey(config.hotkey)
    backend.session = _SESSION

    seen = []
    backend._serve([_signal("Activated", "osta{sv}", (_SESSION, action, 0, {}))], seen.append)

    assert seen == [action]


def test_portal_ignores_activations_for_other_sessions_and_unknown_ids(config, monkeypatch):
    monkeypatch.setattr(hotkey, "_bus_send", lambda *args, **kwargs: ())
    backend = hotkey.PortalHotkey(config.hotkey)
    backend.session = _SESSION

    seen = []
    backend._serve(
        [
            _signal("Activated", "osta{sv}", ("/org/freedesktop/portal/desktop/session/9/other", "speak", 0, {})),
            _signal("Activated", "osta{sv}", (_SESSION, "not-a-shortcut", 0, {})),
        ],
        seen.append,
    )

    assert seen == []


def test_portal_reregisters_and_rebinds_when_the_portal_restarts(config, monkeypatch):
    send, calls = _recorder({
        "CreateSession": {"session_handle": _SESSION},
        "BindShortcuts": {"shortcuts": [("speak", {})]},
    })
    monkeypatch.setattr(hotkey, "_bus_send", send)
    backend = hotkey.PortalHotkey(config.hotkey)
    backend._bind()
    calls.clear()

    restart = _signal(
        "NameOwnerChanged", "sss", (_PORTAL, ":1.1", ":1.2"),
        interface=_BUS, path=_BUS_PATH, sender=_BUS,
    )
    backend._serve([restart], lambda action: None)

    methods = [call.method for call in calls]
    assert methods.count("Register") == 1
    assert methods.count("CreateSession") == 1
    assert methods.count("BindShortcuts") == 1
    register = [call for call in calls if call.method == "Register"][0]
    assert (register.destination, register.path, register.interface, register.signature) == (
        _PORTAL, _REGISTRY_PATH, _REGISTRY_INTERFACE, "sa{sv}")
    assert register.args == (hotkey.APP_ID, {})


def test_portal_ignores_the_portal_owner_leaving(config, monkeypatch):
    send, calls = _recorder({})
    monkeypatch.setattr(hotkey, "_bus_send", send)

    leaving = _signal(
        "NameOwnerChanged", "sss", (_PORTAL, ":1.2", ""),
        interface=_BUS, path=_BUS_PATH, sender=_BUS,
    )
    hotkey.PortalHotkey(config.hotkey)._serve([leaving], lambda action: None)

    assert calls == []


def test_every_match_rule_is_removed_again(monkeypatch):
    send, calls = _recorder({})
    monkeypatch.setattr(hotkey, "_bus_send", send)
    rule = hotkey.MatchRule(type="signal", interface=_BUS, member="NameOwnerChanged")

    hotkey._subscribe(rule)
    assert hotkey._subscribed == [rule]
    hotkey._unsubscribe(rule)

    assert hotkey._subscribed == []
    assert [call.method for call in calls] == ["AddMatch", "RemoveMatch"]
    assert calls[0].args == calls[1].args == (rule.serialise(),)
    assert [call.signature for call in calls] == ["s", "s"]


def test_a_failed_add_match_records_nothing(monkeypatch):
    calls = []

    def send(destination, path, interface, method, signature=None, *args):
        calls.append(method)
        raise hotkey.PortalError("the bus refused the subscription")

    monkeypatch.setattr(hotkey, "_bus_send", send)
    rule = hotkey.MatchRule(type="signal", interface=_BUS, member="NameOwnerChanged")

    with pytest.raises(hotkey.PortalError):
        hotkey._subscribe(rule)

    assert hotkey._subscribed == []
    assert calls == ["AddMatch"]  # nothing was accepted, so nothing is left to remove


def test_listen_releases_a_rule_when_the_second_subscribe_fails(config, monkeypatch):
    monkeypatch.setattr(hotkey, "_connection", lambda: _StubConnection())
    calls = []

    def send(destination, path, interface, method, signature=None, *args):
        calls.append(method)
        if calls.count("AddMatch") == 2:
            raise hotkey.PortalError("the bus refused the second subscription")
        return ()

    monkeypatch.setattr(hotkey, "_bus_send", send)
    backend = hotkey.PortalHotkey(config.hotkey)

    with pytest.raises(hotkey.PortalError):
        backend._listen(lambda action: None)

    assert hotkey._subscribed == []
    assert [method for method in calls if method in ("AddMatch", "RemoveMatch")] == [
        "AddMatch", "AddMatch", "RemoveMatch",
    ]


# --- the receive loop -------------------------------------------------------------------


def test_portal_messages_yields_each_signal_then_stops_on_the_flag(config):
    backend = hotkey.PortalHotkey(config.hotkey)
    activated, owners = deque(), deque()
    script = [
        _signal("Activated", "osta{sv}", (_SESSION, "speak", 0, {})),
        _signal("NameOwnerChanged", "sss", (_PORTAL, ":1.1", ":1.2"),
                interface=_BUS, path=_BUS_PATH, sender=_BUS),
    ]

    class Connection:
        def recv_messages(self, timeout=None):
            if not script:
                raise TimeoutError
            message = script.pop(0)
            member = message.header.fields.get(HeaderFields.member)
            (activated if member == "Activated" else owners).append(message)

    messages = backend._messages(Connection(), activated, owners)
    assert next(messages).body[1] == "speak"
    assert next(messages).body[0] == _PORTAL

    backend._stop.set()  # as stop() would, from another thread
    with pytest.raises(StopIteration):
        next(messages)


def test_portal_messages_returns_when_the_bus_says_nothing_and_stop_is_set(config):
    backend = hotkey.PortalHotkey(config.hotkey)

    class Connection:
        def recv_messages(self, timeout=None):
            backend._stop.set()  # the wall-clock wait is bounded, so stop() is always seen
            raise TimeoutError

    assert list(backend._messages(Connection(), deque(), deque())) == []


def test_portal_messages_returns_when_the_bus_goes_away(config):
    backend = hotkey.PortalHotkey(config.hotkey)

    class Connection:
        def recv_messages(self, timeout=None):
            raise OSError("the bus is gone")

    assert list(backend._messages(Connection(), deque(), deque())) == []
