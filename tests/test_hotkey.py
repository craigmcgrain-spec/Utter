import pytest

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
