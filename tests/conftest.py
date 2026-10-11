"""Shared fixtures: every path-touching test runs against a temporary XDG root."""

import numpy as np
import pytest


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
