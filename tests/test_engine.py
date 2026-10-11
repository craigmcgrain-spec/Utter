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

from utter.config import Speech
from utter.tts.chunk import TooLongError
from utter.tts.engine import SynthService


class FakeModel:
    """The tests inject `synthesize`, so the model is only here for voice_names()."""

    def session(self):
        raise AssertionError("the injected synthesize must be used instead")

    def voice_names(self):
        return ["af_heart"]


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


def test_render_to_wav_writes_the_pipeline_output(player, tmp_path):
    service = make_service(lambda text: np.ones(24000, dtype=np.float32), player)
    out = tmp_path / "offline.wav"
    service.render_to_wav("One thing.", out)
    with wave.open(str(out)) as handle:
        assert (handle.getframerate(), handle.getnchannels()) == (24000, 1)
        assert handle.getnframes() == 24000


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
