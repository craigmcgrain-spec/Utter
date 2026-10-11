"""The orchestrator: one hotkey press becomes audio, and a newer press cancels an older one.

``SynthService`` owns a single worker thread and a latest-wins ``pending`` slot of depth
one, holding the already-split units of the newest request. ``speak()`` normalises and
splits **synchronously, on the caller's thread** -- both are pure and microseconds long --
so the two failure modes reach the caller rather than a worker: a ``TooLongError``
propagates for the caller to show the refusal (spec §5), and text that splits to no units
returns having changed nothing, so a stray whitespace-only selection cannot interrupt
speech that is already playing. Only when there are units does ``speak()`` bump the
generation, publish them as ``pending``, call ``player.stop()`` and wake the worker.
Calling ``player.stop()`` there is deliberate: it drops the previous utterance's queued
audio *and* clears a previous stream-failure flag, so a new utterance is a fresh attempt.

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
``speak()``. Any other exception is reported the same way and abandons that text. A
handler that itself raises out of ``on_error`` or ``on_state`` is logged through this
module's logger and ignored: a failing tray or overlay callback must not take the worker
down with it, because every later ``speak()`` would then be silently unprocessed.

``status()`` is the contract the control protocol and the tray both read:
``generation`` (the monotonic int), ``state`` (the player's state name), ``voice``,
``speed`` and ``paused``. ``played_units()`` is the test-visible record of the units of
the latest ``speak()`` that the player actually accepted.

``render_to_wav()`` is the offline sibling of ``speak()``: the same normalise → split →
synthesise sequence, writing the concatenated audio to a 24 kHz mono 16-bit PCM WAV and
enqueueing nothing, so ``utter speak --save-wav`` can render without an output device and
without disturbing playback. The two share ``_prepare()`` and ``_unit_audio()`` precisely so
a headless render cannot drift from what a user hears; they differ only in what consumes the
units (the player, or a list of arrays).
"""

from __future__ import annotations

import logging
import threading
import time
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

import numpy as np

from ..config import Speech
from ..normalize import normalize
from .audio import Player, PlayerState
from .chunk import FIRST_CHARS, UNIT_CHARS, SplitReason, Unit, split_units

logger = logging.getLogger(__name__)
"""Carries the resolved-config, model-load and per-utterance timing lines (spec §9), and
faults raised out of the caller's own callbacks, which must not kill the worker."""

SAMPLE_RATE = 24000
"""What the synthesiser produces and what the output device opens, so a WAV matches both."""

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
        self._pending: list[Unit] | None = None
        self._busy = False
        self._shutdown = False
        self._played: list[str] = []
        self._last_state: str | None = None

        # The resolved config and the model's load time are logged on the first successful
        # synthesis, and again after a failed load (``_use_logged`` is set only once the
        # session exists, so a retry re-logs both lines); ``_spoken_at`` is the instant the
        # newest ``speak()`` published its units, so the worker can report
        # time-to-first-frame against it (spec §9).
        self._use_lock = threading.Lock()
        self._use_logged = False
        self._spoken_at = 0.0

        # Daemon so a service nobody shuts down cannot keep the process alive; shutdown()
        # still joins it.
        self._worker = threading.Thread(target=self._run, name="utter-synth", daemon=True)
        self._worker.start()

    # -- control -----------------------------------------------------------

    def speak(self, text: str) -> None:
        """Normalise, split and speak ``text``, superseding whatever is playing.

        The normalise and split happen here rather than on the worker: they are pure and
        cheap, ``TooLongError`` must reach this caller so it can show the refusal, and text
        with no speakable unit must leave the player completely alone. For text that does
        have units, the generation bump, the new pending value and ``player.stop()`` are one
        critical section, woken only afterwards: a worker woken early could enqueue the new
        text before ``player.stop()`` cleared the queue, leaving it silently dropped.
        """
        units = self._prepare(text)
        if not units:
            return
        with self._cond:
            self._generation += 1
            self._pending = units
            self._played = []
            self._player.stop()
            # Stamped with the other publication state rather than after the lock is
            # dropped: the worker may read the pending units the instant it is woken, so
            # t=0 for ``ttfirst`` has to be fixed first. speak() returns microseconds later.
            self._spoken_at = time.monotonic()
            self._cond.notify_all()

    def render_to_wav(self, text: str, path: Path) -> None:
        """Synthesise ``text`` to a 24 kHz mono 16-bit PCM WAV, playing nothing.

        The same normalise → split → synthesise sequence ``speak()`` runs, minus the
        playback-only concerns: no superseding, no inter-unit pause silence, and no player
        call at all, so this works headless and leaves whatever is playing alone. The
        concatenation happens before the file is opened, so a failure part-way leaves no
        half-written WAV behind, and a fault other than "this unit was empty" propagates to
        the caller rather than being reported and dropped: a truncated file with a success
        status would be a lie.
        """
        chunks = []
        for unit in self._prepare(text):
            audio = self._unit_audio(unit)
            if audio is not None:
                chunks.append(audio)
        audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        pcm = np.clip(np.rint(audio * 32767.0), -32768, 32767).astype("<i2")
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(pcm.tobytes())

    def stop(self) -> None:
        """Silence immediately and drop any text still being worked on."""
        with self._cond:
            self._generation += 1
            self._pending = None
            self._player.stop()
        self._publish(self._player.state().name)

    def pause(self) -> None:
        """Stop the player writing; synthesis continues until the queue fills."""
        self._player.pause()
        self._publish(self._player.state().name)

    def resume(self) -> None:
        """Continue writing at the next unwritten frame, without re-synthesising."""
        self._player.resume()
        self._publish(self._player.state().name)

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

    # -- pipeline ----------------------------------------------------------

    def _prepare(self, text: str) -> list[Unit]:
        """Normalise and split on the caller's thread: the one sequence every entry point
        starts from, so playback and an offline render cannot drift apart. Raises
        ``TooLongError`` for text over ``speech.max_chars``."""
        prepared = normalize(text) if self._speech.normalize else text
        return split_units(prepared, max_chars=self._speech.max_chars)

    def _unit_audio(self, unit: Unit) -> Optional[np.ndarray]:
        """Synthesise one unit, or ``None`` when there is nothing in it to synthesise.

        A ``ValueError`` from the synthesiser means exactly that (spec §9), and it is
        reported here; every other exception belongs to the caller, which decides whether
        the rest of the text is still worth synthesising.
        """
        try:
            return self._synthesize(unit.text)
        except ValueError as exc:
            self._report(exc)
            return None

    # -- worker ------------------------------------------------------------

    def _run(self) -> None:
        while True:
            with self._cond:
                while self._pending is None and not self._shutdown:
                    self._cond.wait()
                if self._shutdown:
                    return
                units, generation = self._pending, self._generation
                started = self._spoken_at
                self._pending = None
                self._busy = True
            self._run_units(units, generation, started)
            if self._resting():
                # Playback can outlast the last enqueue, and the resting state is the one
                # worth reporting: wait the tail out rather than publish the transient
                # PLAYING. A newer text, a pause or shutdown ends the wait immediately.
                while (not self._player.wait_idle(0.2) and self._resting()
                       and self._player.state() is not PlayerState.PAUSED):
                    pass
            if self._resting():
                # Published before ``_busy`` clears, so a caller of wait_idle() that returns
                # here has already seen this transition.
                self._publish(self._player.state().name)
            with self._cond:
                self._busy = False
                self._cond.notify_all()

    def _run_units(self, units: list[Unit], generation: int, started: float) -> None:
        """Synthesise and enqueue one utterance, then log its §11 measurements.

        ``started`` is when ``speak()`` published the units. The logged ``units`` is the
        number actually enqueued (not everything planned, which an early return would
        overstate); ``audio`` is the seconds of audio actually enqueued (the raw unit arrays,
        so it excludes the trailing pause the player is handed); ``synth`` is the wall time
        spent inside the synthesiser for every unit attempted; and ``ttfirst`` is the time
        from ``started`` to the first frame the player accepted, logged as ``-`` when nothing
        was accepted so no consumer can read a negative time. The line is emitted however the
        loop ends -- completion, supersession, a dead player or a broken synthesiser.
        """
        accepted = 0
        produced = 0.0
        synthesis = 0.0
        ttfirst = -1.0
        try:
            for unit in units:
                if self._stale(generation):
                    return
                try:
                    begin = time.monotonic()
                    audio = self._unit_audio(unit)
                    synthesis += time.monotonic() - begin
                except Exception as exc:
                    # Systemically broken synthesis: report it and abandon the rest of this
                    # text. (``ValueError`` never reaches here — it means just this unit was
                    # empty, which ``_unit_audio`` has already handled.)
                    self._report(exc)
                    return
                if audio is None:
                    continue
                if self._stale(generation):
                    return
                if not self._player.enqueue(audio, self._pause_for(unit.reason)):
                    return
                accepted += 1
                if ttfirst < 0.0:
                    ttfirst = time.monotonic() - started
                produced += len(audio) / SAMPLE_RATE
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
                # The unit was accepted, so playback is starting. A drained player may already
                # read IDLE here (a short unit can finish before this line), which is why the
                # transition is derived from the enqueue rather than re-read: only an active
                # pause changes what an accepted unit means.
                self._publish(
                    "PAUSED" if self._player.state() is PlayerState.PAUSED else "PLAYING"
                )
        finally:
            if ttfirst < 0.0:
                logger.info(
                    "utterance units=%d audio=%.2f synth=%.2f ttfirst=-",
                    accepted, produced, synthesis,
                )
            else:
                logger.info(
                    "utterance units=%d audio=%.2f synth=%.2f ttfirst=%.3f",
                    accepted, produced, synthesis, ttfirst,
                )

    def _default_synthesize(self, unit_text: str) -> np.ndarray:
        """Synthesise one unit with the shared Kokoro wrapper, trim on, 24 kHz float32."""
        kokoro = self._warm_session()
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

    def _warm_session(self) -> object:
        """The one warm Kokoro wrapper, logging its first use (spec §9).

        The config and the model load are logged here, through this module's logger, on the
        first successful synthesis and again after a failed load: they are not per-utterance,
        so they cannot ride the per-utterance line. ``unit_chars``/``first_chars`` are the caps
        ``split_units`` applies (``_prepare`` passes only ``max_chars``, so these defaults are
        the effective ones).
        """
        with self._use_lock:
            if self._use_logged:
                return self._model.session()
            speech = self._speech
            logger.info(
                "config voice=%s speed=%s lang=%s normalize=%s max_chars=%d "
                "unit_chars=%d first_chars=%d",
                speech.voice, speech.speed, speech.lang, speech.normalize,
                speech.max_chars, UNIT_CHARS, FIRST_CHARS,
            )
            began = time.monotonic()
            try:
                kokoro = self._model.session()
            except Exception:
                # Emit the load line on the failure path too, and leave the flag unset: a
                # corrupt or missing model must not mark the config as logged and make a
                # later successful retry in this process silently skip both lines.
                logger.exception("model load failed after %.2fs", time.monotonic() - began)
                raise
            logger.info("model load=%.2fs", time.monotonic() - began)
            self._use_logged = True
            return kokoro

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
        """Report a synthesis fault, surviving a handler that itself raises (spec §9)."""
        if self._on_error is None:
            return
        try:
            self._on_error(str(exc))
        except Exception:
            logger.exception("on_error handler failed")

    def _publish(self, state: str) -> None:
        """Hand a playback-state transition to ``on_state``, with no lock held.

        Called on a unit being accepted (``PLAYING``, or ``PAUSED`` while a pause is
        active), when the worker drains to rest, and by ``pause``/``resume``/``stop``, with
        a repeated state suppressed. It may fire **on the worker thread**, because playback
        can end by itself; a handler that touches the GUI must therefore marshal it onto
        the GUI thread (a queued Qt signal, or ``QMetaObject.invokeMethod``) and must return
        promptly -- the worker-side calls happen while the worker is still marked busy. A
        handler that raises is logged through this module's logger and ignored (spec §9),
        and the transition is not retried.
        """
        if self._on_state is None:
            return
        with self._cond:
            if state == self._last_state:
                return
            self._last_state = state
        try:
            self._on_state(state)
        except Exception:
            logger.exception("on_state handler failed")
