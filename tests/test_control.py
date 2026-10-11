"""ControlServer and the client: JSON-lines over a Unix socket, owner-only, restartable.

The fixtures point ``UTTER_RUNTIME_DIR`` at a temporary root, so no test touches the
user's real socket; ``server.stop()`` joins its threads and unlinks the path, so a test
may start and stop several servers in one process.
"""

from utter import paths
from utter.control import ControlServer, is_running, send


def test_request_response_round_trip(tmp_xdg):
    server = ControlServer(paths.socket_path(), {"status": lambda req: {"state": "IDLE"}})
    server.start()
    try:
        assert send({"command": "status"}) == {"ok": True, "state": "IDLE"}
    finally:
        server.stop()


def test_socket_is_not_world_readable(tmp_xdg):
    server = ControlServer(paths.socket_path(), {})
    server.start()
    try:
        assert paths.socket_path().stat().st_mode & 0o077 == 0
    finally:
        server.stop()


def test_stale_socket_file_is_replaced(tmp_xdg):
    paths.socket_path().parent.mkdir(parents=True, exist_ok=True)
    paths.socket_path().write_text("")
    server = ControlServer(paths.socket_path(), {"status": lambda req: {}})
    server.start()
    try:
        assert send({"command": "status"}) == {"ok": True}
    finally:
        server.stop()


def test_unknown_command_is_an_error(tmp_xdg):
    server = ControlServer(paths.socket_path(), {})
    server.start()
    try:
        assert send({"command": "nope"})["ok"] is False
    finally:
        server.stop()


def test_send_returns_none_when_nothing_is_listening(tmp_xdg):
    assert send({"command": "status"}) is None
    assert is_running() is False
