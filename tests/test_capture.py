from types import SimpleNamespace

import pytest

import utter.capture as capture
from utter.capture import Selection, SelectionTooLong, read_selection, sanitize


def test_primary_wins_when_present():
    got = read_selection(session_type="wayland", primary=lambda: "hello world",
                         clipboard=lambda: "other")
    assert got == Selection("hello world", "primary")


def test_clipboard_used_when_primary_is_empty():
    got = read_selection(session_type="wayland", primary=lambda: None,
                         clipboard=lambda: "from clipboard")
    assert got == Selection("from clipboard", "clipboard")


def test_nothing_selected_returns_none():
    assert read_selection(session_type="wayland", primary=lambda: "   ",
                          clipboard=lambda: "") is None


def test_sanitize_strips_nuls_newlines_and_collapses_space():
    assert sanitize("  a\u0000b  \n c\t\tc \n") == "ab c c"


def test_sanitize_rejects_whitespace_only():
    assert sanitize(" \t\n ") is None


def test_over_long_selection_is_refused():
    with pytest.raises(SelectionTooLong):
        read_selection(session_type="x11", primary=lambda: "x" * 10, max_chars=5)


def test_wayland_primary_uses_wl_paste_primary(monkeypatch):
    calls = []
    monkeypatch.setattr(capture.subprocess, "run",
                        lambda argv, **kw: calls.append(argv)
                        or SimpleNamespace(stdout="sel", returncode=0))
    assert capture.default_primary("wayland")() == "sel"
    assert calls[0][:3] == ["wl-paste", "--primary", "--no-newline"]


def test_missing_binary_is_nothing_selected(monkeypatch):
    def boom(argv, **kw):
        raise FileNotFoundError("wl-paste")
    monkeypatch.setattr(capture.subprocess, "run", boom)
    assert capture.default_primary("wayland")() is None


def test_non_zero_exit_is_nothing_selected(monkeypatch):
    monkeypatch.setattr(capture.subprocess, "run",
                        lambda argv, **kw: SimpleNamespace(stdout="", returncode=1))
    assert capture.default_primary("wayland")() is None


def test_undecodable_output_is_not_fatal(monkeypatch):
    # subprocess.run is pinned to utf-8 with errors="replace", so undecodable bytes arrive as
    # U+FFFD; sanitize drops them, so such a selection reads as nothing selected, not a crash.
    monkeypatch.setattr(capture.subprocess, "run",
                        lambda argv, **kw: SimpleNamespace(stdout="\ufffd\ufffd", returncode=0))
    assert sanitize(capture.default_primary("wayland")()) is None
