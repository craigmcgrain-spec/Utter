"""The control channel: JSON-lines over a Unix socket, so ``utter`` is a client of the tray.

The running tray process owns the warm model and the output device; every other ``utter``
invocation must talk to it rather than load a second model. This module is that channel and
nothing else: no authentication beyond the socket's own mode, no history, no subscriptions.

**Protocol.** One request object per line, one response object per line, UTF-8, newline
terminated. A response is always a JSON object with ``"ok": bool`` and, on failure,
``"error": str``. The request names a command and carries the rest of its fields; the
handlers are supplied by the caller (Task 13's application wiring), so this module knows no
commands. Command names and their payloads live in the CLI's docstring.

**The path decides nothing by itself.** A socket path left behind by a process that died has
no listener behind it, so both :func:`is_running` and :meth:`ControlServer.start` use a
*connect probe* rather than a stat: a path that refuses the connection is stale and is
unlinked before rebinding, while a path that accepts one belongs to a live instance and a
second server refuses to start rather than steal it. A probe that *times out* leaves the
question open, so it too refuses: unlinking a socket someone may still be serving would
strand every later client.

**Owner-only from the start.** The server binds with a ``0o177`` umask so the path is created
``0600`` rather than chmod-ed afterwards; there is no window in which another local user
could connect, which a chmod after ``bind()`` would leave open.

**Stopping is complete.** :meth:`ControlServer.stop` joins the serving thread, shuts down the
live connections so their per-connection threads end, joins those with a bounded timeout --
never indefinitely, since ``stop()`` runs on the caller's thread and a handler parked on an
answer only that thread can give must not park it -- and unlinks the path, so a test, or a
restart, can start and stop servers repeatedly without leaking a thread or leaving a stale
file. It only ever removes a path this server bound: :meth:`start` failing (another instance
owns the socket) can therefore never be followed by a ``stop()`` that deletes the live
instance's socket.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import Callable

from . import paths

logger = logging.getLogger(__name__)

TIMEOUT = 2.0
"""Default deadline, in seconds, for a connect probe and for one request's response."""

ACCEPT_TIMEOUT = 0.2
"""How long ``accept()`` may block before the serving thread re-checks whether to stop."""

STOP_JOIN_TIMEOUT = 0.5
"""How long :meth:`ControlServer.stop` waits for a connection thread before letting it go.

Whatever the bound, ``stop()`` is reached from the GUI thread (the app's shutdown path), so it
must never join a connection thread without one: a handler parked on something only the GUI
thread can answer -- the selection read the app marshals there -- cannot be unblocked by
shutting its socket down, and waiting for it would park the GUI thread that owes the answer.
A straggler is left running instead: every connection thread is a daemon, and it unwinds by
itself, because a handler parked on the GUI thread's answer gives up at its own bounded
deadline (which the app keeps below the client's)."""


def _is_private_dir(directory: Path) -> bool:
    """True when ``directory`` is ours and no one else can write into it.

    The fallback runtime directory is a predictable ``/tmp/utter-<uid>`` that another local user
    can create first. A socket in a directory someone else owns or can write to can be replaced,
    and a client connecting to the replacement hands it the selected text, so both sides refuse
    it: the server will not bind there and the client will not talk there.
    """
    try:
        st = os.stat(directory)
    except OSError:
        return False
    # Group write is allowed: Fedora's umask 002 makes a user's own directories group-writable
    # for their private group, which is no one else.
    return st.st_uid == os.getuid() and st.st_mode & 0o002 == 0



def _connect(path: Path, timeout: float) -> socket.socket:
    """Return a connected client socket, or raise ``OSError`` (a timeout included)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
    except BaseException:
        sock.close()
        raise
    return sock


def is_running(path: Path | None = None) -> bool:
    """True when something accepts on ``path`` right now — a connect probe, not a stat."""
    path = paths.socket_path() if path is None else Path(path)
    if not _is_private_dir(path.parent):
        return False
    try:
        probe = _connect(path, TIMEOUT)
    except OSError:
        return False
    probe.close()
    return True


def send(command: dict, path: Path | None = None, timeout: float = TIMEOUT) -> dict | None:
    """Send one command and return the response, or ``None`` when nothing answers.

    ``None`` covers every way this can fail to produce an answer within ``timeout``: no
    listener, a peer that closes without replying, a reply that is not a JSON object, a
    timeout on any of the three steps, or a socket directory another user could have planted
    (see :func:`_is_private_dir`). A client must never raise out of a dead peer, and the
    caller's decision (delegate or fall back) is the same in all of those cases.
    """
    path = paths.socket_path() if path is None else Path(path)
    if not _is_private_dir(path.parent):
        return None
    deadline = time.monotonic() + timeout
    try:
        sock = _connect(path, timeout)
    except OSError:
        return None
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None  # the connect alone spent the whole budget
        # One deadline for all three steps: connect (above), write, read. Without this the
        # write would inherit the full timeout again and a peer that stops reading could hold
        # the call for twice the documented bound.
        sock.settimeout(remaining)
        sock.sendall(json.dumps(command).encode("utf-8") + b"\n")
        line = _read_line(sock, deadline)
    except OSError:
        return None
    finally:
        sock.close()
    try:
        response = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return response if isinstance(response, dict) else None


def _read_line(sock: socket.socket, deadline: float) -> bytes:
    """Read one newline-terminated line, or raise ``OSError`` past the deadline."""
    received = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("no response before the deadline")
        sock.settimeout(remaining)
        block = sock.recv(65536)
        if not block:
            raise ConnectionError("the server closed the connection without a response")
        received += block
        if b"\n" in block:
            return bytes(received).split(b"\n", 1)[0]


class ControlServer:
    """Serve the JSON-lines protocol on a Unix socket, one thread per connection."""

    def __init__(self, path: Path, handlers: dict[str, Callable[[dict], dict]]) -> None:
        self._path = Path(path)
        self._handlers = dict(handlers)
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._connections: dict[socket.socket, threading.Thread] = {}
        self._lock = threading.Lock()
        self._running = False

    def start(self) -> None:
        """Bind and serve, replacing a stale path and refusing a live one.

        Raises ``RuntimeError`` when the path is owned by a running instance or when the
        probe cannot tell (a timeout), because either way taking the path would break it.
        """
        if self._running:
            raise RuntimeError(f"{self._path} is already being served by this instance")
        self._prepare_path()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous = os.umask(0o177)  # 0o777 & ~0o177 == 0o600: owner-only as it is created
        try:
            listener.bind(str(self._path))
            listener.listen(8)
        except OSError as exc:
            # Another server may have bound the same path between our probe and our bind (a
            # real race, not a stale file), so this is the same conflict the probe reports --
            # a caller must not have to tell "someone else owns this socket" apart from a raw
            # errno, nor guess whether the path is usable.
            listener.close()
            raise RuntimeError(f"cannot serve on {self._path}: {exc}") from exc
        finally:
            os.umask(previous)
        listener.settimeout(ACCEPT_TIMEOUT)
        self._listener = listener
        self._running = True
        self._thread = threading.Thread(target=self._serve, name="utter-control", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop serving, join every thread and unlink the socket. Idempotent."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            connections = dict(self._connections)
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        for connection in connections:
            # shutdown(), not close(): it is what unblocks a per-connection thread already
            # parked in recv(), so the join below cannot hang on an idle client.
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        for thread in connections.values():
            # Bounded, not indefinite: this runs on the GUI thread, and a handler waiting for an
            # answer only the GUI thread can give would otherwise park it (see STOP_JOIN_TIMEOUT).
            thread.join(timeout=STOP_JOIN_TIMEOUT)
        with self._lock:
            self._connections.clear()
        self._path.unlink(missing_ok=True)

    # -- serving -----------------------------------------------------------

    def _prepare_path(self) -> None:
        """Create the runtime directory, refuse one that is not private, then clear or refuse
        the socket path."""
        directory = self._path.parent
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(f"cannot create {directory}: {exc}") from exc
        if not _is_private_dir(directory):
            raise RuntimeError(
                f"refusing to serve in {directory}: it must be owned by this user and "
                "writable by no one else"
            )
        if not (self._path.exists() or self._path.is_symlink()):
            return
        try:
            probe = _connect(self._path, TIMEOUT)
        except TimeoutError as exc:
            raise RuntimeError(f"cannot tell whether {self._path} is live: {exc}") from exc
        except OSError:
            logger.info("replacing the stale socket at %s", self._path)
            self._path.unlink()
            return
        probe.close()
        raise RuntimeError(f"another utter instance already owns {self._path}")

    def _serve(self) -> None:
        while self._running:
            listener = self._listener
            if listener is None:
                return
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return  # stop() closed the listener
            self._spawn(connection)

    def _spawn(self, connection: socket.socket) -> None:
        thread = threading.Thread(
            target=self._handle, args=(connection,), name="utter-control-client", daemon=True
        )
        with self._lock:
            if not self._running:
                connection.close()
                return
            self._connections[connection] = thread
        thread.start()

    def _handle(self, connection: socket.socket) -> None:
        """Answer every request on one connection until it ends."""
        try:
            reader, writer = connection.makefile("rb"), connection.makefile("wb")
            try:
                for line in reader:
                    response = self._dispatch(line)
                    if response is None:  # a blank line: nothing to answer
                        continue
                    writer.write(json.dumps(response).encode("utf-8") + b"\n")
                    writer.flush()
            finally:
                reader.close()
                writer.close()
        except OSError:
            pass  # the client vanished, or stop() shut the connection down
        finally:
            connection.close()
            with self._lock:
                self._connections.pop(connection, None)

    def _dispatch(self, line: bytes) -> dict | None:
        """Turn one request line into the response object it deserves."""
        if not line.strip():
            return None
        try:
            request = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {"ok": False, "error": "request is not valid JSON"}
        if not isinstance(request, dict):
            return {"ok": False, "error": "request must be a JSON object"}
        name = request.get("command")
        # Not a bare ``self._handlers.get(name)``: a JSON array or object for ``command`` is
        # unhashable, so the lookup raises inside this connection's thread and the client gets a
        # closed socket instead of the error it is owed.
        handler = self._handlers.get(name) if isinstance(name, str) else None
        if handler is None:
            return {"ok": False, "error": f"unknown command: {name!r}"}
        try:
            # A handler that reports a refusal returns {"ok": False, ...} itself; its own
            # verdict wins over the default. An escaping exception is a bug in the handler,
            # so it is logged and reported rather than killing the connection.
            return {"ok": True, **(handler(request) or {})}
        except Exception as exc:
            logger.exception("control handler %r failed", name)
            return {"ok": False, "error": str(exc)}
