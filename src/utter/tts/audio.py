"""The playback player: one PortAudio output stream behind a bounded unit queue.

``SynthService`` enqueues one synthesised unit at a time as ``(audio, pause)`` pairs;
the writer thread drains that queue and writes 512-frame ``(N, 1)`` float32 blocks,
followed by ``pause`` seconds of silence. The final block of a unit is short and is
never zero-padded, because the engine asserts that concatenating the written blocks
reproduces the audio exactly, sample for sample.

Pause is by omission: ``pause()`` sets an event the writer checks before each block,
so the open stream underruns to silence, and ``resume()`` continues at the next
unwritten frame with no re-synthesis and no repeated frame. ``pause()`` and
``stop()`` both take the same ``_io_lock`` the writer holds across a block write and
the writer re-tests both the pause event and the generation inside that lock, so once
either call returns no block can be written afterwards: ``stop`` cannot race the
writer into writing after the flush.

``enqueue`` returns False only when its own utterance was cancelled — a ``stop()``
landed while the call was blocked on a full queue — or when the player is failed; a
fresh call after a ``stop()`` records the new generation and returns True, which is
how the next utterance starts. A write failure reports through ``on_error``, drops the
queue, sets the failed flag and goes back to ``IDLE``; ``stop(flush=True)`` clears the
flag so the next utterance is a fresh attempt. There is deliberately no frame-position
accessor, no volume control, no resampling and no reopening logic beyond the flag.
"""

import enum
import queue
import threading
import time
from typing import Callable, Optional, Protocol

import numpy as np

BLOCK_FRAMES = 512
"""Frames per write. The last block of a unit may be shorter and is never padded."""


class StreamLike(Protocol):
    """The slice of ``sounddevice.OutputStream`` this module depends on."""

    active: bool

    def write(self, data: np.ndarray) -> None: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


class PlayerState(enum.Enum):
    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"


class Player:
    """Owns the output stream and one writer thread; callers push units at it."""

    def __init__(
        self,
        samplerate: int = 24000,
        stream_factory: Optional[Callable[[], StreamLike]] = None,
        max_units: int = 8,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._samplerate = samplerate
        self._stream_factory = stream_factory
        self._on_error = on_error
        self._queue: queue.Queue = queue.Queue(maxsize=max_units)
        self._cond = threading.Condition()
        self._io_lock = threading.Lock()
        self._paused = threading.Event()
        self._generation = 0
        self._failed = False
        self._shutdown = False
        self._busy = False
        self._state = PlayerState.IDLE
        self._thread: Optional[threading.Thread] = None
        self.stream: Optional[StreamLike] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Open the output stream and start the writer thread."""
        self.stream = self._stream_factory() if self._stream_factory else self._open_stream()
        self.stream.start()
        self._thread = threading.Thread(target=self._run, name="utter-player", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        """Stop the writer thread (joining it) and close the stream."""
        with self._cond:
            self._shutdown = True
            self._paused.clear()
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join()
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()

    def _open_stream(self) -> StreamLike:
        # Imported lazily: the module stays importable without PortAudio, and the
        # tests always inject a stream through ``stream_factory``.
        import sounddevice as sd

        return sd.OutputStream(samplerate=self._samplerate, channels=1, dtype="float32")

    # -- control -----------------------------------------------------------

    def enqueue(self, audio: np.ndarray, pause: float) -> bool:
        """Queue one unit of audio plus the silence that follows it.

        Returns False when this utterance was cancelled by a ``stop()`` that landed
        while the call was blocked on a full queue, or when the player has failed.
        A call made *after* a ``stop()`` records the new generation and returns True.
        """
        with self._cond:
            if self._failed:
                return False
            generation = self._generation
            if not self._paused.is_set():
                self._state = PlayerState.PLAYING
        item = (generation, np.asarray(audio, dtype=np.float32).reshape(-1), float(pause))
        while True:
            try:
                self._queue.put(item, timeout=0.1)
            except queue.Full:
                with self._cond:
                    if self._failed or self._generation != generation:
                        return False
                continue
            with self._cond:
                self._cond.notify_all()  # wake the writer; it re-tests the queue
                return self._generation == generation

    def pause(self) -> None:
        """Stop the writer writing; the stream stays open and underruns to silence."""
        with self._cond:
            self._paused.set()
            self._state = PlayerState.PAUSED
            self._cond.notify_all()
        with self._io_lock:
            pass  # wait out any block already in flight; no further write can start

    def resume(self) -> None:
        """Continue writing at the next unwritten frame, without re-synthesising."""
        with self._cond:
            self._paused.clear()
            if self._state is PlayerState.PAUSED:
                if self._busy or not self._queue.empty():
                    self._state = PlayerState.PLAYING
                else:
                    self._state = PlayerState.IDLE
            self._cond.notify_all()

    def stop(self, flush: bool = True) -> None:
        """Silence immediately: cancel the in-flight utterance and go ``IDLE``.

        ``flush`` controls whether the queued-but-unplayed audio is discarded; the
        generation always advances, so the stop is audible either way. The failed
        flag is cleared, making the next ``enqueue`` a fresh attempt.
        """
        with self._cond:
            self._generation += 1
            self._failed = False
            self._paused.clear()
            if flush:
                self._clear_queue()
            self._state = PlayerState.IDLE
            self._cond.notify_all()
        with self._io_lock:
            pass  # the writer re-tests the generation under this lock before writing

    def state(self) -> PlayerState:
        with self._cond:
            return self._state

    def wait_idle(self, timeout: float) -> bool:
        """True once the queue is drained and the last frame has been written."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._busy or not self._queue.empty():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    # -- writer thread -----------------------------------------------------

    def _run(self) -> None:
        while True:
            with self._cond:
                if self._shutdown:
                    return
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    self._busy = False
                    self._settle_state()
                    self._cond.notify_all()
                    self._cond.wait(0.05)
                    continue
                self._busy = True
                if not self._paused.is_set():
                    self._state = PlayerState.PLAYING
            self._play(item)
            with self._cond:
                self._busy = False
                self._settle_state()
                self._cond.notify_all()

    def _play(self, item: tuple) -> None:
        generation, audio, pause = item
        position = 0
        total = audio.shape[0]
        while position < total:
            end = min(position + BLOCK_FRAMES, total)
            if not self._write(audio[position:end], generation):
                return
            position = end
        silence = int(pause * self._samplerate)
        position = 0
        while position < silence:
            end = min(position + BLOCK_FRAMES, silence)
            if not self._write(np.zeros(end - position, dtype=np.float32), generation):
                return
            position = end

    def _write(self, block: np.ndarray, generation: int) -> bool:
        """Write one ``(N, 1)`` block. False if the utterance was cancelled or failed."""
        data = block.reshape(-1, 1)
        while True:
            with self._io_lock:
                if self._shutdown or self._failed or generation != self._generation:
                    return False
                if not self._paused.is_set():
                    try:
                        self.stream.write(data)
                    except Exception as exc:  # device loss, route change, ...
                        self._fail(exc)
                        return False
                    return True
            if not self._park(generation):
                return False

    def _park(self, generation: int) -> bool:
        """Wait out a pause. False when the utterance was cancelled while parked."""
        while self._paused.is_set():
            with self._cond:
                if self._shutdown or self._failed or generation != self._generation:
                    return False
            time.sleep(0.01)
        return True

    def _fail(self, exc: Exception) -> None:
        with self._cond:
            self._failed = True
            self._clear_queue()
            self._state = PlayerState.IDLE
            self._cond.notify_all()
        if self._on_error is not None:
            self._on_error(str(exc))

    def _settle_state(self) -> None:
        """Recompute the resting state; a pause always wins over idle/playing."""
        self._state = PlayerState.PAUSED if self._paused.is_set() else PlayerState.IDLE

    def _clear_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return
