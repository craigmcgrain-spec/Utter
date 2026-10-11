"""Shared fixtures: every path-touching test runs against a temporary XDG root."""

import os

# Before any Qt import anywhere in the suite: there is no display on this machine, and the
# tray/overlay tests must never need one. ``setdefault`` lets a developer override it.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

_APP = None
"""Holds the one ``QApplication`` for the process.

A second ``QApplication`` aborts the interpreter, so the fixture is session-scoped and a
module-level reference keeps the instance from being collected: dropping the last Python
reference to a ``QApplication`` tears down the Qt application and a later widget aborts.
"""


@pytest.fixture(scope="session")
def qapp():
    """The one ``QApplication`` for the process, offscreen so no display is needed."""
    global _APP
    from PyQt6.QtWidgets import QApplication

    _APP = QApplication.instance() or QApplication([])
    return _APP


@pytest.fixture
def config(tmp_xdg):
    """A ``Config`` whose paths land under the temporary XDG root."""
    from utter import paths
    from utter.config import Config

    return Config.load(paths.config_path())


class FakeService:
    """A ``SynthService`` stand-in that records what the UI asked it to do.

    ``spoken`` is the ordered list of texts handed to ``speak``; ``calls`` is the
    ordered list of method names, so a test can assert that, say, ``stop`` reached
    the service without caring about arguments. It offers the whole surface the tray,
    overlay and control handlers touch, so a UI test never needs a model, a device or
    a worker thread. ``status`` and ``voice_names`` return copies, so a caller that
    mutates what it got cannot corrupt the fake.
    """

    def __init__(self, voices=("af_heart", "af_bella", "am_michael"), state="IDLE"):
        self.spoken: list[str] = []
        self.calls: list[str] = []
        self._voices = list(voices)
        self._status = {
            "generation": 0,
            "state": state,
            "voice": "af_heart",
            "speed": 1.0,
            "paused": state == "PAUSED",
        }

    def speak(self, text):
        self.calls.append("speak")
        self.spoken.append(text)

    def stop(self):
        self.calls.append("stop")

    def pause(self):
        self.calls.append("pause")

    def resume(self):
        self.calls.append("resume")

    def status(self):
        self.calls.append("status")
        return dict(self._status)

    def voice_names(self):
        self.calls.append("voice_names")
        return list(self._voices)

    def wait_idle(self, timeout):
        self.calls.append("wait_idle")
        return True

    def shutdown(self):
        self.calls.append("shutdown")


@pytest.fixture
def fake_service():
    """Records what the UI asked for: ``.spoken``, ``.calls`` and the service methods."""
    return FakeService()


class FakeStream:
    """A ``StreamLike`` stand-in that records every block it is asked to write.

    The player and its tests share this one fake: the ``player`` fixture wires it
    into the ``Player`` through ``stream_factory``, and the failure test subclasses
    it. It lives here rather than in ``test_audio.py`` so the fixture can find it.
    """

    def __init__(self):
        self.frames: list[np.ndarray] = []
        self.started = self.closed = False
        self.active = True

    def write(self, data):
        self.frames.append(np.array(data, copy=True))

    def start(self):
        self.started = True

    def stop(self):
        pass

    def close(self):
        self.closed = True

    @property
    def written_frames(self):
        return sum(len(frame) for frame in self.frames)


@pytest.fixture
def player():
    """A started ``Player`` wired to a ``FakeStream``, shut down after the test."""
    from utter.tts.audio import Player

    stream = FakeStream()
    instance = Player(stream_factory=lambda: stream)
    instance.start()
    try:
        yield instance
    finally:
        instance.shutdown()


@pytest.fixture
def tmp_xdg(tmp_path, monkeypatch):
    """Point every UTTER_* XDG override at a fresh temporary root and return it."""
    root = tmp_path / "xdg"
    overrides = {
        "UTTER_CONFIG_HOME": "config",
        "UTTER_STATE_HOME": "state",
        "UTTER_DATA_HOME": "data",
        "UTTER_RUNTIME_DIR": "runtime",
    }
    for var, subdir in overrides.items():
        directory = root / subdir
        directory.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(var, str(directory))
    return root
