"""Player behaviour against a fake output stream.

``FakeStream`` lives in ``conftest.py`` because the shared ``player`` fixture (also
there, and reused by the engine, tray and app tests) wires it into the ``Player``.
Tests that need to hold a write open subclass it locally so a block is provably in
flight rather than merely likely to be.
"""

import threading
import time

import numpy as np

from conftest import FakeStream
from utter.tts.audio import Player, PlayerState


def test_written_frames_are_mono_float32(player):
    player.enqueue(np.ones(2400, dtype=np.float32), pause=0.0)
    player.wait_idle(timeout=2.0)
    assert player.stream.frames and all(f.dtype == np.float32 for f in player.stream.frames)
    assert all(f.ndim == 2 and f.shape[1] == 1 for f in player.stream.frames)


def test_pause_stops_writing_and_resume_continues_without_repeating():
    """The writer is held inside a write, so pause() provably lands mid-utterance."""

    class GatedStream(FakeStream):
        def __init__(self):
            super().__init__()
            self.gate = threading.Event()
            self.entered = threading.Event()

        def write(self, data):
            self.entered.set()
            self.gate.wait(5.0)
            super().write(data)

    audio = np.arange(24000, dtype=np.float32)
    stream = GatedStream()
    player = Player(stream_factory=lambda: stream)
    player.start()
    try:
        player.enqueue(audio, pause=0.0)
        assert stream.entered.wait(2.0), "the writer never started writing"

        pauser = threading.Thread(target=player.pause, daemon=True)
        pauser.start()
        # pause() records PAUSED before it waits out the in-flight block, so once the
        # state is visible the pause event is set and the writer cannot advance again.
        deadline = time.monotonic() + 2.0
        while player.state() is not PlayerState.PAUSED:
            assert time.monotonic() < deadline, "pause() never took effect"
        stream.gate.set()  # let the single in-flight block finish; pause() now returns
        pauser.join(2.0)
        assert not pauser.is_alive(), "pause() did not return"

        frozen = stream.written_frames
        assert 0 < frozen < len(audio), "pause must land mid-utterance"
        time.sleep(0.2)
        assert stream.written_frames == frozen, "paused player must not write"
        assert player.state() is PlayerState.PAUSED

        player.resume()
        assert player.wait_idle(timeout=3.0)
        written = np.concatenate([f.ravel() for f in stream.frames])
        assert written.tolist() == audio.tolist(), "resume must continue, not restart"
    finally:
        stream.gate.set()
        player.shutdown()


def test_stop_drops_the_queue_and_accepts_the_next_utterance(player):
    player.enqueue(np.ones(96000, dtype=np.float32), pause=0.0)
    player.stop()
    assert player.state() is PlayerState.IDLE
    dropped = player.stream.written_frames
    time.sleep(0.2)
    assert player.stream.written_frames == dropped, "stop must drop the queued audio"
    assert player.enqueue(np.ones(240, dtype=np.float32), pause=0.0) is True


def test_pause_then_stop_does_not_hang(player):
    player.enqueue(np.ones(24000, dtype=np.float32), pause=0.0)
    player.pause()
    player.stop()
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
        player.stop()
        assert player.enqueue(np.ones(240, dtype=np.float32), pause=0.0) is True
    finally:
        player.shutdown()


def test_enqueue_reports_false_when_the_write_fails_while_it_is_blocked():
    """A failure that frees a full queue must cancel the blocked enqueue, not accept it."""

    class FailingGatedStream(FakeStream):
        def __init__(self):
            super().__init__()
            self.gate = threading.Event()
            self.entered = threading.Event()

        def write(self, data):
            self.entered.set()
            self.gate.wait(5.0)
            raise RuntimeError("device gone")

    stream = FailingGatedStream()
    player = Player(stream_factory=lambda: stream, max_units=1)
    player.start()
    try:
        assert player.enqueue(np.ones(24000, dtype=np.float32), pause=0.0) is True
        assert stream.entered.wait(2.0)
        assert player.enqueue(np.ones(2400, dtype=np.float32), pause=0.0) is True
        result = {}

        def blocked():
            result["r"] = player.enqueue(np.ones(2400, dtype=np.float32), pause=0.0)

        thread = threading.Thread(target=blocked)
        thread.start()
        time.sleep(0.2)  # deliberate: let the blocked enqueue reach its put() on the full queue
        stream.gate.set()  # the write fails, freeing a slot for that blocked put
        thread.join(2.0)
        assert result["r"] is False, "a failed player must not accept the queued unit"
        assert player.state() is PlayerState.IDLE
    finally:
        stream.gate.set()
        player.shutdown()


def test_cancelled_enqueue_does_not_discard_the_next_utterance():
    """A cancelled enqueue must not dispose of a later utterance's queued audio.

    The writer is paused, so it parks holding the first unit without draining the queue;
    a stale enqueue blocks on the full queue; ``stop()`` advances the generation and frees
    the slots, and the stale call's post-``put`` check then sees the change. The fresh
    utterance enqueued in the meantime must survive that check and still reach the stream.
    """
    stream = FakeStream()
    player = Player(stream_factory=lambda: stream, max_units=2)
    player.start()
    try:
        player.pause()  # the writer parks on the unit it already holds, so the queue stays full
        assert player.enqueue(np.ones(24000, dtype=np.float32), pause=0.0) is True
        assert player.enqueue(np.ones(2400, dtype=np.float32), pause=0.0) is True
        assert player.enqueue(np.ones(2400, dtype=np.float32), pause=0.0) is True  # queue now full
        result = {}

        def stale():
            result["r"] = player.enqueue(np.ones(2400, dtype=np.float32), pause=0.0)

        thread = threading.Thread(target=stale)
        thread.start()
        time.sleep(0.2)  # deliberate: let the stale enqueue reach put() on the full queue
        player.stop()  # cancels the utterance and frees the slots the stale call waits on
        fresh = np.arange(2400, dtype=np.float32)
        assert player.enqueue(fresh, pause=0.0) is True
        thread.join(2.0)
        assert result["r"] is False, "the cancelled call must report cancellation"

        assert player.wait_idle(timeout=2.0)
        written = (
            np.concatenate([f.ravel() for f in stream.frames])
            if stream.frames
            else np.zeros(0, dtype=np.float32)
        )
        assert written.tolist() == fresh.tolist(), "the next utterance must still play"
    finally:
        player.shutdown()


def test_error_handler_may_call_stop_without_deadlocking():
    """``on_error`` runs with no player lock held, so a handler may call ``stop()``.

    Every wait is bounded and teardown skips the join when the writer never comes back, so
    the deadlock this pins fails the test instead of hanging the suite.
    """

    class BrokenStream(FakeStream):
        def write(self, data):
            raise RuntimeError("device gone")

    errors = []
    handled = threading.Event()
    player = None

    def handler(message):
        errors.append(message)
        player.stop()  # re-entered _io_lock in the buggy version, deadlocking this thread
        handled.set()

    player = Player(stream_factory=BrokenStream, on_error=handler)
    player.start()
    try:
        assert player.enqueue(np.ones(240, dtype=np.float32), pause=0.0) is True
        assert handled.wait(2.0), "on_error handler deadlocked in stop()"
        assert errors == ["device gone"]
        assert player.wait_idle(timeout=2.0)
        assert player.state() is PlayerState.IDLE
    finally:
        if handled.is_set():
            player.shutdown()
        # else: the writer is stuck in the handler; joining it would hang the suite.


def test_raising_error_handler_does_not_kill_the_writer_thread(caplog):
    """A handler that raises must not take the writer thread down with it."""

    class FailsOnceStream(FakeStream):
        def __init__(self):
            super().__init__()
            self.raised = False

        def write(self, data):
            if not self.raised:
                self.raised = True
                raise RuntimeError("device gone")
            super().write(data)

    stream = FailsOnceStream()
    seen = []
    called = threading.Event()

    def handler(message):
        seen.append(message)
        called.set()
        raise RuntimeError("handler blew up")

    player = Player(stream_factory=lambda: stream, on_error=handler)
    player.start()
    try:
        assert player.enqueue(np.ones(240, dtype=np.float32), pause=0.0) is True
        assert called.wait(2.0), "on_error was never called"
        assert seen == ["device gone"], "the original write failure must still be reported"
        assert player.state() is PlayerState.IDLE
        assert player.wait_idle(timeout=2.0)  # the failed unit is finished, either way
        assert any(
            thread.name == "utter-player" and thread.is_alive()
            for thread in threading.enumerate()
        ), "the writer thread must survive a raising handler"

        player.stop()  # a fresh attempt must be possible after the handler failed
        good = np.arange(2400, dtype=np.float32)
        assert player.enqueue(good, pause=0.0) is True
        assert player.wait_idle(timeout=2.0)
        written = (
            np.concatenate([f.ravel() for f in stream.frames])
            if stream.frames
            else np.zeros(0, dtype=np.float32)
        )
        assert written.tolist() == good.tolist(), "playback must work again"
        assert any(
            record.name == "utter.tts.audio" and "on_error handler raised" in record.getMessage()
            for record in caplog.records
        ), "the handler's own failure must be reported through the module logger"
    finally:
        player.shutdown()


def test_enqueue_reports_playing_immediately():
    """A successful enqueue reports PLAYING before the writer can settle it back to IDLE."""

    class GatedStream(FakeStream):
        def __init__(self):
            super().__init__()
            self.gate = threading.Event()

        def write(self, data):
            self.gate.wait(2.0)
            super().write(data)

    stream = GatedStream()
    player = Player(stream_factory=lambda: stream)
    player.start()
    try:
        assert player.enqueue(np.ones(2400, dtype=np.float32), pause=0.0) is True
        assert player.state() is PlayerState.PLAYING
    finally:
        stream.gate.set()
        player.shutdown()


def test_inter_unit_pause_is_written_as_unpadded_silence(player):
    audio = np.ones(1000, dtype=np.float32)
    player.enqueue(audio, pause=0.25)
    assert player.wait_idle(timeout=2.0)
    written = np.concatenate([f.ravel() for f in player.stream.frames])
    assert len(written) == len(audio) + int(0.25 * 24000)
    assert written[: len(audio)].tolist() == audio.tolist()
    assert len(player.stream.frames[-1]) < 512, "the tail must be short, never zero-padded"
    assert all(f.dtype == np.float32 and f.ndim == 2 and f.shape[1] == 1 for f in player.stream.frames)


def test_a_registered_error_listener_is_told_of_a_write_failure():
    """``add_error_listener`` is how a second owner learns of the same failure.

    The engine registers one because a failure that lands while its worker is parked is
    otherwise invisible to it; both handlers must hear the message.
    """

    class BrokenStream(FakeStream):
        def write(self, data):
            raise RuntimeError("device gone")

    via_on_error, via_listener = [], []
    player = Player(stream_factory=BrokenStream, on_error=via_on_error.append)
    player.add_error_listener(via_listener.append)
    player.start()
    try:
        assert player.enqueue(np.ones(2400, dtype=np.float32), pause=0.0) is True
        assert player.wait_idle(timeout=2.0)
    finally:
        player.shutdown()
    assert via_on_error == ["device gone"]
    assert via_listener == ["device gone"], "the listener must hear the failure too"


def test_a_raising_error_listener_does_not_kill_the_writer_thread():
    """A raising listener is logged, not allowed to take the writer thread down."""

    class BrokenStream(FakeStream):
        def write(self, data):
            raise RuntimeError("device gone")

    called = threading.Event()

    def explode(message):
        called.set()
        raise RuntimeError("listener blew up")

    player = Player(stream_factory=BrokenStream)
    player.add_error_listener(explode)
    player.start()
    try:
        player.enqueue(np.ones(240, dtype=np.float32), pause=0.0)
        assert called.wait(2.0), "the listener was never called"
        assert player.wait_idle(timeout=2.0)
        assert any(
            thread.name == "utter-player" and thread.is_alive()
            for thread in threading.enumerate()
        ), "the writer thread must survive a raising listener"
    finally:
        player.shutdown()
