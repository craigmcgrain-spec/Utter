import pytest
from jeepney import DBusAddress, new_signal

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


def _recorder(answers):
    """A ``_bus_send`` stand-in: records every call and answers from ``answers``."""
    calls = []

    def send(destination, path, interface, method, *args):
        calls.append((method, args))
        answer = answers.get(method, ())
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return send, calls


def _signal(member, signature, body, *, interface=_GLOBAL_SHORTCUTS, path="/org/freedesktop/portal/desktop",
            sender=_PORTAL):
    """A real jeepney signal message, built without a bus."""
    address = DBusAddress(path, bus_name=sender, interface=interface)
    return new_signal(address, member, signature, body)


def test_portal_bind_creates_a_session_and_binds_the_three_triggers(config, monkeypatch):
    send, calls = _recorder({
        "CreateSession": {"session_handle": _SESSION},
        "BindShortcuts": {"speak": {}, "stop": {}, "pause": {}},
    })
    monkeypatch.setattr(hotkey, "_bus_send", send)

    backend = hotkey.PortalHotkey(config.hotkey)
    backend._bind()

    assert backend.session == _SESSION
    assert backend.bound == ["pause", "speak", "stop"]
    assert backend.declined is False
    assert [method for method, _ in calls] == ["CreateSession", "BindShortcuts"]

    create_options = calls[0][1][0]
    assert create_options["session_handle_token"]
    session, entries, parent_window, bind_options = calls[1][1]
    assert session == _SESSION
    assert parent_window == ""
    assert create_options["handle_token"] and bind_options["handle_token"]
    assert create_options["handle_token"] != bind_options["handle_token"]
    assert [(action, options["preferred_trigger"]) for action, options in entries] == [
        ("speak", "LOGO+ALT+s"), ("stop", "LOGO+ALT+x"), ("pause", "LOGO+ALT+p"),
    ]
    assert [options["description"] for _, options in entries] == [
        "Speak selected text", "Stop speaking", "Pause or resume",
    ]


@pytest.mark.parametrize("answer", [
    pytest.param({}, id="empty-set"),
    pytest.param(hotkey.PortalRefused("cancelled"), id="refused-response"),
])
def test_portal_declined_bind_is_reported_and_start_does_not_raise(config, monkeypatch, answer):
    send, _ = _recorder({"CreateSession": {"session_handle": _SESSION}, "BindShortcuts": answer})
    monkeypatch.setattr(hotkey, "_bus_send", send)
    monkeypatch.setattr(hotkey.PortalHotkey, "_listen", lambda self, on_action: None)

    backend = hotkey.PortalHotkey(config.hotkey)
    backend.start(lambda action: None)  # the caller must survive a decline and notify instead
    backend._thread.join(timeout=5)

    assert not backend._thread.is_alive()
    assert backend.declined is True
    assert backend.bound == []
    assert backend.session == _SESSION
    backend.stop()
    assert backend._thread is None


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
        "BindShortcuts": {"speak": {}},
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

    methods = [method for method, _ in calls]
    assert methods.count("Register") == 1
    assert methods.count("CreateSession") == 1
    assert methods.count("BindShortcuts") == 1
    assert [args for method, args in calls if method == "Register"] == [(hotkey.APP_ID, {})]


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
    assert [method for method, _ in calls] == ["AddMatch", "RemoveMatch"]
    assert calls[0][1] == calls[1][1]  # the same rule string goes out and comes back
