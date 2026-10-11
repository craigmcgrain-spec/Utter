"""The orchestrator: one hotkey press becomes audio, and a newer press cancels an older one.

``SynthService`` owns a single worker thread and a latest-wins ``pending`` slot of depth
one. ``speak()`` increments the generation, publishes the new text as ``pending``, calls
``player.stop()`` and wakes the worker; the worker takes ``pending``, normalises it when
``speech.normalize`` is set, splits it into units and synthesises them one at a time into
the player. ``player.stop()`` in ``speak()`` is deliberate: it drops the previous
utterance's queued audio *and* clears a previous stream-failure flag, so a new utterance
is always a fresh attempt.

The generation is checked **before** synthesising each unit and **again after synthesis,
before the unit is enqueued**, so a unit that was in flight when a newer ``speak()``
arrived is dropped rather than heard. A check only at the top of the loop would let the
superseded text play. The one remaining window — the generation changing between the
second check and the player accepting the unit — is closed by inspecting the generation
once more *after* a successful ``enqueue``: a unit that slipped out that way is cancelled
with ``player.stop()`` and never recorded as played.

``player.enqueue`` returning False means that unit's utterance was cancelled (a ``stop()``
landed while the call was blocked on a full queue) or the player has failed, so the worker
abandons the rest of that text rather than synthesising audio nobody will hear. A
``ValueError`` from the synthesiser means "nothing to synthesise" for that unit: it is
reported through ``on_error`` and the loop moves on, leaving the worker alive for the next
``speak()``. Any other exception is reported the same way and abandons that text.

``status()`` is the contract the control protocol and the tray both read:
``generation`` (the monotonic int), ``state`` (the player's state name), ``voice``,
``speed`` and ``paused``. ``played_units()`` is the test-visible record of the units of
the latest ``speak()`` that the player actually accepted.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Callable, Optional

import numpy as np

from ..config import Speech
from ..normalize import normalize
from .audio import Player, PlayerState
from .chunk import SplitReason, split_units

if TYPE_CHECKING:
    # Annotations only: importing ``model`` at runtime would drag onnxruntime into every
    # module that imports this one, and the UI never loads onnxruntime (spec 4.1). Task 8
    # defines the Phonemizer protocol elsewhere; nothing here needs it at runtime.
    from .model import ModelStore
    from .phonemes import Phonemizer


class SynthService:
    """Turn one selection into audio on a worker thread, newest press wins."""

    def __init__(
        self,
        model: "ModelStore",
        player: Player,
        speech: Speech,
        synthesize: Optional[Callable[[str], np.ndarray]] = None,
        phonemizer: Optional["Phonemizer"] = None,
        on_state: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._model = model
        self._player = player
        self._speech = speech
        self._phonemizer = phonemizer
        self._on_state = on_state
        self._on_error = on_error
        self._synthesize = synthesize if synthesize is not None else self._default_synthesize

        self._cond = threading.Condition()
        self._generation = 0
        self._pending: str | None = None
        self._busy = False
        self._shutdown = False
        self._played: list[str] = []
        self._last_state: str | None = None

        # Daemon so a service nobody shuts down cannot keep the process alive; shutdown()
        # still joins it.
        self._worker = threading.Thread(target=self._run, name="utter-synth", daemon=True)
        self._worker.start()

    # -- control -----------------------------------------------------------

    def speak(self, text: str) -> None:
        """Supersede whatever is playing and speak ``text`` instead.

        The generation bump, the new pending value and ``player.stop()`` are published
        under one lock, and the worker is woken only afterwards: a worker that woke early
        could enqueue the new text before ``player.stop()`` cleared the queue, leaving the
        fresh utterance silently dropped.
        """
        with self._cond:
            self._generation += 1
            self._pending = text
            self._played = []
            self._player.stop()
            self._cond.notify_all()

    def stop(self) -> None:
        """Silence immediately and drop any text still being worked on."""
        with self._cond:
            self._generation += 1
            self._pending = None
            self._player.stop()
        self._publish_state()

    def pause(self) -> None:
        """Stop the player writing; synthesis continues until the queue fills."""
        self._player.pause()
        self._publish_state()

    def resume(self) -> None:
        """Continue writing at the next unwritten frame, without re-synthesising."""
        self._player.resume()
        self._publish_state()

    def shutdown(self) -> None:
        """Drop pending work, silence the player and join the worker thread.

        ``player.stop()`` comes before the join so a worker parked inside ``enqueue`` on a
        full queue is released instead of blocking the join forever. Closing the output
        device stays with whoever created the player.
        """
        with self._cond:
            self._shutdown = True
            self._pending = None
            self._cond.notify_all()
        self._player.stop()
        self._worker.join()

    # -- read-only ---------------------------------------------------------

    def status(self) -> dict:
        """The five contract keys the control protocol and the tray both read."""
        state = self._player.state()
        with self._cond:
            generation = self._generation
        return {
            "generation": generation,
            "state": state.name,
            "voice": self._speech.voice,
            "speed": self._speech.speed,
            "paused": state is PlayerState.PAUSED,
        }

    def voice_names(self) -> list[str]:
        """The sorted voice ids, straight from the model store (never building a session)."""
        return self._model.voice_names()

    def played_units(self) -> list[str]:
        """The units of the latest ``speak()`` that the player accepted, in order."""
        with self._cond:
            return list(self._played)

    def wait_idle(self, timeout: float) -> bool:
        """True once the worker is idle and the player has written its last frame."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while (self._pending is not None or self._busy) and not self._shutdown:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
        return self._player.wait_idle(max(0.0, deadline - time.monotonic()))

    # -- worker ------------------------------------------------------------

    def _run(self) -> None:
        while True:
            with self._cond:
                while self._pending is None and not self._shutdown:
                    self._cond.wait()
                if self._shutdown:
                    return
                text, generation = self._pending, self._generation
                self._pending = None
                self._busy = True
            self._run_text(text, generation)
            with self._cond:
                self._busy = False
                self._cond.notify_all()
            if self._resting():
                # Playback can outlast the last enqueue: wait it out so the state published
                # is the resting one rather than the transient PLAYING. A newer text stops
                # the player, which ends this wait immediately.
                while not self._player.wait_idle(0.2) and self._resting():
                    pass
                if self._resting():  # a newer text may have arrived while draining
                    self._publish_state()

    def _run_text(self, text: str, generation: int) -> None:
        try:
            prepared = normalize(text) if self._speech.normalize else text
            units = split_units(prepared, max_chars=self._speech.max_chars)
        except Exception as exc:  # TooLongError and any other refusal: report, drop this text
            self._report(exc)
            return
        for unit in units:
            if self._stale(generation):
                return
            try:
                audio = self._synthesize(unit.text)
            except ValueError as exc:  # nothing to synthesise for this unit
                self._report(exc)
                continue
            except Exception as exc:  # systemically broken synthesis: report, drop the text
                self._report(exc)
                return
            if self._stale(generation):
                return
            if not self._player.enqueue(audio, self._pause_for(unit.reason)):
                return
            with self._cond:
                superseded = self._shutdown or self._generation != generation
                if not superseded:
                    self._played.append(unit.text)
            if superseded:
                # The generation changed between the check above and the player accepting
                # the unit, so it slipped out unstamped: cancel it rather than let a
                # superseded unit be heard. Nothing of the newer text is enqueued yet --
                # this worker threads one text at a time.
                self._player.stop()
                return
            self._publish_state()

    def _default_synthesize(self, unit_text: str) -> np.ndarray:
        """Synthesise one unit with the shared Kokoro wrapper, trim on, 24 kHz float32."""
        kokoro = self._model.session()
        speech = self._speech
        if self._phonemizer is not None:
            return kokoro.create(
                self._phonemizer.phonemize(unit_text, speech.lang),
                voice=speech.voice, speed=speech.speed, lang=speech.lang,
                trim=True, is_phonemes=True,
            )[0]
        return kokoro.create(
            unit_text, voice=speech.voice, speed=speech.speed, lang=speech.lang, trim=True
        )[0]

    def _pause_for(self, reason: SplitReason) -> float:
        """Sentence ends get the longer silence; clause and hard wraps the shorter one."""
        if reason is SplitReason.SENTENCE:
            return self._speech.pause_sentence
        return self._speech.pause_clause

    def _stale(self, generation: int) -> bool:
        with self._cond:
            return self._shutdown or generation != self._generation

    def _resting(self) -> bool:
        """True when nothing is queued for the worker and it has not been shut down."""
        with self._cond:
            return self._pending is None and not self._shutdown

    def _report(self, exc: BaseException) -> None:
        if self._on_error is not None:
            self._on_error(str(exc))

    def _publish_state(self) -> None:
        """Hand a changed player state to ``on_state``, outside every lock.

        The callback may run on the worker thread (playback can end by itself), so a
        handler that touches the GUI is responsible for marshalling it.
        """
        if self._on_state is None:
            return
        state = self._player.state().name
        with self._cond:
            if state == self._last_state:
                return
            self._last_state = state
        self._on_state(state)
