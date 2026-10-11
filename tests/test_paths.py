import os
from pathlib import Path

from utter import paths


def test_config_path_honours_utter_override(tmp_xdg):
    assert paths.config_path() == tmp_xdg / "config" / "utter" / "config.toml"


def test_socket_path_is_under_runtime_dir(tmp_xdg):
    assert paths.socket_path() == tmp_xdg / "runtime" / "utter.sock"


def test_log_path_is_under_state_directory(tmp_xdg):
    assert paths.log_path() == tmp_xdg / "state" / "utter" / "utter.log"


def test_an_absolute_xdg_variable_is_used_when_there_is_no_override(tmp_xdg, monkeypatch):
    monkeypatch.delenv("UTTER_STATE_HOME")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_xdg / "state2"))
    assert paths.log_path() == tmp_xdg / "state2" / "utter" / "utter.log"


def test_a_relative_xdg_variable_is_ignored(tmp_xdg, monkeypatch):
    # The basedir spec calls a relative value invalid, and the control socket makes the reason
    # concrete: the tray resolves the socket path when it binds, and `utter speak` resolves it
    # again from its own working directory, so `XDG_RUNTIME_DIR=runtime` puts the two in
    # different places -- which reads as "not running" and cold-starts a second engine.
    monkeypatch.delenv("UTTER_RUNTIME_DIR")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "runtime")
    assert paths.socket_path() == Path(f"/tmp/utter-{os.getuid()}") / "utter.sock"

    monkeypatch.delenv("UTTER_CONFIG_HOME")
    monkeypatch.setenv("XDG_CONFIG_HOME", "config")
    assert paths.config_path() == Path("~/.config").expanduser() / "utter" / "config.toml"
