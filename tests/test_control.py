"""ControlServer and the client: JSON-lines over a Unix socket, owner-only, restartable.

The fixtures point ``UTTER_RUNTIME_DIR`` at a temporary root, so no test touches the
user's real socket; ``server.stop()`` joins its threads and unlinks the path, so a test
may start and stop several servers in one process.
"""

import socket
import threading
import time

import pytest

from utter import control, paths
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


def test_a_second_server_refuses_a_live_path(tmp_xdg):
    server = ControlServer(paths.socket_path(), {"status": lambda req: {"n": 1}})
    server.start()
    try:
        second = ControlServer(paths.socket_path(), {})
        with pytest.raises(RuntimeError, match="utter.sock"):
            second.start()
        second.stop()  # a server that never started must not unlink the live socket
        assert send({"command": "status"}) == {"ok": True, "n": 1}
    finally:
        server.stop()


def test_a_bind_collision_is_a_runtime_error_not_a_bare_oserror(tmp_xdg, monkeypatch):
    # Two servers can pass the connect probe before either binds; the loser then gets
    # EADDRINUSE from bind(), which the caller must be able to tell apart from "something
    # else broke", so it is reported as the same RuntimeError the probe raises.
    server = ControlServer(paths.socket_path(), {"status": lambda req: {"n": 1}})
    server.start()
    try:
        monkeypatch.setattr(ControlServer, "_prepare_path", lambda self: None)
        loser = ControlServer(paths.socket_path(), {})
        with pytest.raises(RuntimeError, match="utter.sock"):
            loser.start()
        loser.stop()
        assert send({"command": "status"}) == {"ok": True, "n": 1}
    finally:
        server.stop()


def test_send_keeps_one_deadline_when_the_peer_stops_reading(tmp_xdg, monkeypatch):
    """A peer that accepts and then never reads blocks the write until the socket buffers
    fill; the documented timeout is one budget for connect, write and read, not one each."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(paths.socket_path()))
    listener.listen(1)
    held = []
    threading.Thread(target=lambda: held.append(listener.accept()), daemon=True).start()

    real_connect = control._connect

    def slow_connect(path, timeout):
        time.sleep(0.4)  # a connect that spends most of a 0.5 s deadline, as a busy server does
        return real_connect(path, timeout)

    monkeypatch.setattr(control, "_connect", slow_connect)
    started = time.monotonic()
    assert send({"command": "x", "filler": "0" * (8 << 20)}, timeout=0.5) is None
    elapsed = time.monotonic() - started
    try:
        assert elapsed < 0.75, f"send() took {elapsed:.2f}s for a 0.5s deadline"
    finally:
        listener.close()


def test_stop_does_not_wait_for_a_parked_handler(tmp_xdg):
    """``stop()`` runs on the caller's thread -- the app's is the GUI thread.

    A handler parked on an answer only that thread can give (the selection read the app
    marshals onto it) cannot be woken by shutting its socket down, so joining it without a
    timeout would park the very thread that owes the answer.
    """
    started, release = threading.Event(), threading.Event()

    def hang(request):
        started.set()
        release.wait(3.0)
        return {"ok": True}

    server = ControlServer(paths.socket_path(), {"hang": hang})
    server.start()
    client = threading.Thread(
        target=lambda: send({"command": "hang"}, timeout=0.2), daemon=True
    )
    client.start()
    try:
        assert started.wait(2.0), "the handler must be parked before stop() is called"
        began = time.monotonic()
        server.stop()
        elapsed = time.monotonic() - began
    finally:
        release.set()
        client.join(timeout=5)

    assert elapsed < 2.0, f"stop() waited {elapsed:.2f}s for a parked handler"
    assert not paths.socket_path().exists(), "the path is still unlinked before the straggler goes"
