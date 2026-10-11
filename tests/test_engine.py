"""SynthService: one worker thread, a latest-wins pending slot, a generation counter.

Every test injects ``synthesize``, so the real model and ``onnxruntime`` are never
touched; ``FakeModel`` stands in only for ``voice_names()``. The ``player`` fixture comes
from ``conftest.py`` and is function-scoped, because tests here replace ``enqueue`` and
``stop`` on it, and ``build_service`` shuts every service down on teardown so no test
leaves a worker thread behind.
"""

import logging
import re
import threading
import time
import wave

import numpy as np
import pytest

from conftest import FakeStream
from utter.config import Speech
from utter.tts.audio import Player
from utter.tts.chunk import TooLongError
from utter.tts.engine import SynthService


class FakeModel:
    """The tests inject `synthesize`, so the model is only here for voice_names()."""

    def session(self):
        raise AssertionError("the injected synthesize must be used instead")

    def voice_names(self):
        return ["af_heart"]


class FakeKokoro:
    """Stands in for the warm Kokoro wrapper: one second of audio per create() call."""

    def create(self, text, **kwargs):
        return [np.ones(24000, dtype=np.float32)]


def make_service(synth, player, **kwargs):
    speech = Speech(pause_sentence=0.25, pause_clause=0.12)
    return SynthService(model=FakeModel(), player=player, speech=speech,
                        synthesize=synth, **kwargs)


@pytest.fixture
def build_service(player):
    """Build services bound to the test's player; shut each one down (twice) afterwards.

    Every service built here is shut down on teardown -- including when the test fails --
    so no test relies on the process exiting to reap a worker thread. The second
    ``shutdown()`` pins that the call is idempotent.
    """
    built = []

    def build(synth, **kwargs):
        instance = make_service(synth, player, **kwargs)
        built.append(instance)
        return instance

    yield build

    for instance in built:
        instance.shutdown()
        instance.shutdown()


def pauses_for(player, build_service, text):
    """Speak `text` with enqueue recorded instead of played, and return the pauses."""
    pauses = []
    player.enqueue = lambda audio, pause: pauses.append(pause) or True
    service = build_service(lambda unit: np.zeros(240, dtype=np.float32))
    service.speak(text)
    service.wait_idle(timeout=5.0)
    return pauses


def test_units_are_synthesised_in_order(player, build_service):
    seen = []
    def synth(text):
        seen.append(text)
        return np.zeros(240, dtype=np.float32)
    service = build_service(synth)
    service.speak("One thing. Two things.")
    service.wait_idle(timeout=5.0)
    assert seen == ["One thing.", "Two things."]


def test_pauses_come_from_split_reason(player, build_service):
    # Both shapes are pinned by the chunking tests, so this depends on no new policy:
    # two short sentences stay whole (SENTENCE -> 0.25), a leading clause is cut (CLAUSE -> 0.12).
    assert pauses_for(player, build_service, "One thing. Another thing.") == [0.25, 0.25]
    clause_first = pauses_for(player, build_service,
                              "In short, this sentence runs long enough to be cut by the "
                              "eighty character cap, and then it continues for a while.")
    assert clause_first[0] == 0.12


def test_second_speak_supersedes_the_first(player, build_service):
    started = threading.Event()
    release = threading.Event()
    def synth(text):
        if text.startswith("First"):
            started.set()
            release.wait(3.0)
        return np.zeros(240, dtype=np.float32)
    service = build_service(synth)
    service.speak("First sentence here now.")
    assert started.wait(3.0)
    service.speak("Second sentence here now.")
    release.set()
    service.wait_idle(timeout=5.0)
    played = service.played_units()
    assert not any(u.startswith("First") for u in played), "superseded text must never be heard"
    assert any(u.startswith("Second") for u in played)


def test_synthesis_error_is_reported_and_service_survives(player, build_service):
    errors = []
    calls = []
    def synth(text):
        calls.append(text)
        if len(calls) == 1:
            raise ValueError("Nothing to synthesize")
        return np.zeros(240, dtype=np.float32)
    service = build_service(synth, on_error=errors.append)
    service.speak("Anything at all here.")
    service.wait_idle(timeout=5.0)
    assert errors and "Nothing to synthesize" in errors[0]
    assert service.status()["state"] == "IDLE"
    # Surviving means the worker is still there to take the next text: a worker that died
    # on the error would leave this one pending forever.
    service.speak("A second sentence now.")
    assert service.wait_idle(timeout=5.0), "the worker must still be alive after the error"
    assert service.played_units() == ["A second sentence now."]


def test_status_reports_generation_and_state(player, build_service):
    service = build_service(lambda text: np.zeros(240, dtype=np.float32))
    before = service.status()["generation"]
    service.speak("One sentence only.")
    service.wait_idle(timeout=5.0)
    assert service.status()["generation"] == before + 1
    assert set(service.status()) >= {"generation", "state", "voice", "speed", "paused"}


def test_currency_amount_is_never_split_across_units(player, build_service):
    seen = []
    service = build_service(lambda text: seen.append(text) or np.zeros(24, dtype=np.float32))
    service.speak("It costs $1,234.56 today, which is a great deal more than it used to be.")
    service.wait_idle(timeout=5.0)
    assert not any("$" in unit for unit in seen), "normalisation must run before chunking"
    assert any("dollars" in unit for unit in seen)
    assert not any(unit.strip().startswith("234") for unit in seen)


def test_unit_less_text_leaves_playback_alone(player, build_service):
    stops = []
    original_stop = player.stop
    player.stop = lambda: stops.append(True) or original_stop()
    service = build_service(lambda text: np.zeros(240, dtype=np.float32))
    before = service.status()["generation"]
    service.speak("   ")
    service.speak("...")
    assert stops == [], "a selection with nothing to say must not interrupt playback"
    assert service.status()["generation"] == before


def test_over_long_text_is_refused_to_the_caller(player, build_service):
    service = build_service(lambda text: np.zeros(240, dtype=np.float32))
    with pytest.raises(TooLongError):
        service.speak("word " * 5000)


def test_state_callback_reports_transitions_without_duplicates(player, build_service):
    states = []
    service = build_service(lambda text: np.zeros(240, dtype=np.float32),
                            on_state=states.append)
    service.speak("One thing.")
    service.wait_idle(timeout=5.0)
    assert states[0] == "PLAYING"
    assert states[-1] == "IDLE"
    assert all(a != b for a, b in zip(states, states[1:])), "no duplicate transitions"


def test_a_raising_callback_is_reported_and_the_worker_survives(player, build_service, caplog):
    def explode(message):
        raise RuntimeError("callback exploded")
    calls = []
    def synth(text):
        calls.append(text)
        if len(calls) == 1:
            raise ValueError("Nothing to synthesize")
        return np.zeros(240, dtype=np.float32)
    service = build_service(synth, on_state=explode, on_error=explode)
    service.speak("Anything at all here.")
    assert service.wait_idle(timeout=5.0)
    service.speak("A second sentence now.")
    assert service.wait_idle(timeout=5.0)
    assert service.played_units() == ["A second sentence now."], "a failing handler must not kill it"
    assert "on_error handler failed" in caplog.text
    assert "on_state handler failed" in caplog.text


def test_shutdown_is_idempotent_and_wait_idle_returns_promptly(player, build_service):
    service = build_service(lambda text: np.zeros(240, dtype=np.float32))
    service.speak("One sentence only.")
    assert service.wait_idle(timeout=5.0)
    service.shutdown()
    service.shutdown()  # idempotent: a second call must not raise or hang
    started = time.monotonic()
    assert service.wait_idle(timeout=5.0) is True
    assert time.monotonic() - started < 1.0, "wait_idle after shutdown must not burn its timeout"
    assert service.status()["state"] == "IDLE"


def test_render_to_wav_writes_the_pipeline_output(player, build_service, tmp_path):
    # Built through the fixture, so the service's worker thread is joined at teardown like
    # every other service in this file.
    service = build_service(lambda text: np.ones(24000, dtype=np.float32))
    out = tmp_path / "offline.wav"
    service.render_to_wav("One thing.", out)
    with wave.open(str(out)) as handle:
        assert (handle.getframerate(), handle.getnchannels()) == (24000, 1)
        assert handle.getnframes() == 24000


def test_a_broken_synthesiser_is_logged_with_its_traceback(player, build_service, caplog):
    """Spec §9: a worker exception is logged as well as reported, so the log explains it.

    Only the notification carries the reason otherwise, and nothing else records which unit
    broke or why; the worker must survive the fault and take the next text.
    """
    def synth(text):
        raise RuntimeError("model exploded")

    errors = []
    service = build_service(synth, on_error=errors.append)
    with caplog.at_level(logging.INFO, logger="utter.tts.engine"):
        service.speak("Anything at all here.")
        assert service.wait_idle(timeout=5.0)
    records = [
        record for record in caplog.records
        if record.name == "utter.tts.engine"
        and record.getMessage().startswith("synthesis failed")
    ]
    assert errors and "model exploded" in errors[0], "the user still gets the reason"
    assert records, "the failure must reach the log"
    assert records[0].exc_info, "and must carry its traceback"


def test_resumed_playback_still_ends_in_idle(player, build_service):
    """Pause during the drain, resume, and playback must still be reported as finished.

    The worker's drain wait ended the moment the player was paused, so the worker went idle while
    queued audio remained; the player then finished that audio on its own after a resume, with
    nobody left to publish the resting state. The last published transition stayed ``PLAYING``,
    which is what the tray tooltip and the overlay read -- the overlay stayed on screen over
    playback that had ended. (``status()`` reads the player live, so it was the one thing that
    knew the truth, which is why the published transitions are what this test asserts.)
    """

    class GatedStream(FakeStream):
        """Holds the write open, so playback is provably still running when the pause lands."""

        def __init__(self):
            super().__init__()
            self.gate = threading.Event()
            self.entered = threading.Event()

        def write(self, data):
            self.entered.set()
            self.gate.wait(5.0)
            super().write(data)

    states = []
    stream = GatedStream()
    gated_player = Player(stream_factory=lambda: stream)
    gated_player.start()
    service = SynthService(
        model=FakeModel(),
        player=gated_player,
        speech=Speech(),
        synthesize=lambda text: np.ones(24000, dtype=np.float32),
        on_state=states.append,
    )
    try:
        service.speak("One thing. Two things.")
        assert stream.entered.wait(2.0), "playback never started writing"
        # Both units are enqueued and the worker is waiting out the tail, which is the state a
        # pause has to interrupt for this to test anything.
        deadline = time.monotonic() + 2.0
        while len(service.played_units()) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(service.played_units()) == 2, "the worker never finished enqueueing"

        service.pause()
        assert states[-1] == "PAUSED"
        stream.gate.set()  # the in-flight block finishes; the player parks while paused
        time.sleep(0.2)
        service.resume()
        assert service.wait_idle(3.0), "resumed playback never drained"

        deadline = time.monotonic() + 2.0
        while states[-1] != "IDLE" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert states[-1] == "IDLE", f"playback ended but was never reported: {states}"
        assert service.status()["state"] == "IDLE"
    finally:
        stream.gate.set()
        service.shutdown()
        gated_player.shutdown()


def test_utterance_is_logged_with_units_audio_synth_and_ttfirst(player, build_service, caplog):
    """Spec §9: one INFO line per utterance carries the four §11 measurements."""
    service = build_service(lambda text: np.ones(24000, dtype=np.float32))
    with caplog.at_level(logging.INFO, logger="utter.tts.engine"):
        service.speak("One thing.")
        assert service.wait_idle(timeout=5.0)
    lines = [r.getMessage() for r in caplog.records if r.name == "utter.tts.engine"]
    utterance = [line for line in lines if line.startswith("utterance ")]
    assert len(utterance) == 1, f"exactly one utterance line per spoken text: {lines}"
    # 24000 frames at 24000 Hz is one second of audio; the four fields must all be present.
    assert re.search(
        r"^utterance units=1 audio=1\.00 synth=\d+\.\d+ ttfirst=\d+\.\d+$", utterance[0]
    ), utterance[0]


def test_utterance_line_counts_only_enqueued_units_and_omits_missing_ttfirst(
    player, build_service, caplog
):
    """An utterance nothing accepted logs zero units and no negative time-to-first-frame."""
    def synth(text):
        raise ValueError("Nothing to synthesize")
    service = build_service(synth)
    with caplog.at_level(logging.INFO, logger="utter.tts.engine"):
        service.speak("Anything at all here.")
        assert service.wait_idle(timeout=5.0)
    lines = [
        r.getMessage() for r in caplog.records
        if r.name == "utter.tts.engine" and r.getMessage().startswith("utterance ")
    ]
    assert len(lines) == 1, lines
    assert lines[0].startswith("utterance units=0 audio=0.00 synth="), lines[0]
    assert lines[0].endswith("ttfirst=-"), lines[0]


def test_warm_session_logs_config_and_load_again_after_a_failed_load(player, caplog):
    """A first load that raises must leave the logged flag unset, so a retry logs both lines."""
    class FlakyModel:
        def __init__(self):
            self.calls = 0

        def session(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("corrupt model")
            return FakeKokoro()

        def voice_names(self):
            return []

    errors = []
    service = SynthService(model=FlakyModel(), player=player, speech=Speech(),
                           on_error=errors.append)
    try:
        with caplog.at_level(logging.INFO, logger="utter.tts.engine"):
            service.speak("Hello there.")
            assert service.wait_idle(timeout=5.0)
            service.speak("Hello there.")
            assert service.wait_idle(timeout=5.0)
    finally:
        service.shutdown()
    assert errors and "corrupt model" in errors[0]
    lines = [r.getMessage() for r in caplog.records if r.name == "utter.tts.engine"]
    assert sum(line.startswith("config ") for line in lines) == 2, lines
    assert sum(line.startswith("model load=") for line in lines) == 1, lines


def test_synthesis_speaks_in_the_language_of_the_current_voice(player):
    """``lang = "auto"`` follows the voice, including a voice changed between utterances."""
    langs = []

    class RecordingKokoro:
        def create(self, text, **kwargs):
            langs.append((kwargs["voice"], kwargs["lang"]))
            return [np.zeros(240, dtype=np.float32)]

    class Model:
        def session(self):
            return RecordingKokoro()

        def voice_names(self):
            return []

    speech = Speech(voice="bf_emma")
    service = SynthService(model=Model(), player=player, speech=speech)
    try:
        service.speak("Hello there.")
        assert service.wait_idle(timeout=5.0)
        speech.voice = "ff_siwis"  # what the tray's voice item does
        service.speak("Bonjour.")
        assert service.wait_idle(timeout=5.0)
        speech.lang = "en-us"
        service.speak("Hello again.")
        assert service.wait_idle(timeout=5.0)
    finally:
        service.shutdown()
    assert langs == [("bf_emma", "en-gb"), ("ff_siwis", "fr-fr"), ("ff_siwis", "en-us")]


def test_a_device_failure_while_paused_publishes_idle():
    """A write failure ends in IDLE even when the worker never sees it.

    The worker publishes IDLE when it drains or when ``enqueue`` fails, but a failure that
    lands while it is parked -- the player paused, then resumed onto a dead device -- is never
    observed by that path, so the published state stayed PLAYING and the overlay would sit on
    screen over playback that had already ended. The player's failure notification is the one
    signal that fires for every timing, so the engine publishes IDLE from it too.
    """

    class GatedFailStream(FakeStream):
        """Blocks the first write so a pause can land mid-utterance, then fails on demand."""

        def __init__(self):
            super().__init__()
            self.gate = threading.Event()
            self.entered = threading.Event()
            self.fail = False

        def write(self, data):
            self.entered.set()
            if self.fail:
                raise RuntimeError("device gone")
            self.gate.wait(5.0)
            super().write(data)

    states = []
    stream = GatedFailStream()
    failing_player = Player(stream_factory=lambda: stream)
    failing_player.start()
    service = SynthService(
        model=FakeModel(),
        player=failing_player,
        speech=Speech(),
        synthesize=lambda text: np.ones(96000, dtype=np.float32),
        on_state=states.append,
    )
    try:
        service.speak("One long unit to pause inside.")
        assert stream.entered.wait(2.0), "playback never started writing"
        service.pause()
        stream.gate.set()  # the in-flight block finishes; the player parks while paused
        deadline = time.monotonic() + 2.0
        while (not states or states[-1] != "PAUSED") and time.monotonic() < deadline:
            time.sleep(0.01)
        assert states[-1] == "PAUSED"
        time.sleep(0.2)  # let the worker drain to its parked wait before the device dies

        stream.fail = True
        service.resume()
        deadline = time.monotonic() + 2.0
        while states[-1] != "IDLE" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert states == ["PLAYING", "PAUSED", "PLAYING", "IDLE"], states
        assert failing_player.state().name == "IDLE"
    finally:
        stream.gate.set()
        service.shutdown()
        failing_player.shutdown()
