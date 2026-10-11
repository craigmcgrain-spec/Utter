"""Player behaviour against a fake output stream.

``FakeStream`` lives in ``conftest.py`` because the shared ``player`` fixture (also
there, and reused by the engine, tray and app tests) wires it into the ``Player``.
"""

import time

import numpy as np

from conftest import FakeStream
from utter.tts.audio import Player, PlayerState


def test_written_frames_are_mono_float32(player):
    player.enqueue(np.ones(2400, dtype=np.float32), pause=0.0)
    player.wait_idle(timeout=2.0)
    assert player.stream.frames and all(f.dtype == np.float32 for f in player.stream.frames)
    assert all(f.ndim == 2 and f.shape[1] == 1 for f in player.stream.frames)


def test_pause_stops_writing_and_resume_continues_without_repeating(player):
    audio = np.arange(24000, dtype=np.float32)
    player.enqueue(audio, pause=0.0)
    player.pause()
    frozen = player.stream.written_frames
    time.sleep(0.2)
    assert player.stream.written_frames == frozen, "paused player must not write"
    assert player.state() is PlayerState.PAUSED
    player.resume()
    player.wait_idle(timeout=3.0)
    written = np.concatenate([f.ravel() for f in player.stream.frames])
    assert written.tolist() == audio.tolist(), "resume must continue, not restart"


def test_stop_drops_the_queue_and_accepts_the_next_utterance(player):
    player.enqueue(np.ones(96000, dtype=np.float32), pause=0.0)
    player.stop(flush=True)
    assert player.state() is PlayerState.IDLE
    dropped = player.stream.written_frames
    time.sleep(0.2)
    assert player.stream.written_frames == dropped, "stop must drop the queued audio"
    assert player.enqueue(np.ones(240, dtype=np.float32), pause=0.0) is True


def test_pause_then_stop_does_not_hang(player):
    player.enqueue(np.ones(24000, dtype=np.float32), pause=0.0)
    player.pause()
    player.stop(flush=True)
    assert player.state() is PlayerState.IDLE


def test_stream_failure_stops_cleanly_and_leaves_the_player_retryable_after_stop():
    class BrokenStream(FakeStream):
        def write(self, data):
            raise RuntimeError("device gone")

    errors = []
    player = Player(stream_factory=BrokenStream, on_error=errors.append)
    player.start()
    try:
        player.enqueue(np.ones(2400, dtype=np.float32), pause=0.0)
        player.wait_idle(timeout=2.0)
        assert errors and "device gone" in errors[0]
        assert player.state() is PlayerState.IDLE
        assert player.enqueue(np.ones(240, dtype=np.float32), pause=0.0) is False
        player.stop(flush=True)
        assert player.enqueue(np.ones(240, dtype=np.float32), pause=0.0) is True
    finally:
        player.shutdown()
