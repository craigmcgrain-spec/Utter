"""SynthService: one worker thread, a latest-wins pending slot, a generation counter.

Every test injects ``synthesize``, so the real model and ``onnxruntime`` are never
touched; ``FakeModel`` stands in only for ``voice_names()``. The ``player`` fixture comes
from ``conftest.py`` and is function-scoped, because two tests here replace ``enqueue``
and one replaces ``stop`` on it.
"""

import threading

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


def pauses_for(player, text):
    """Speak `text` with enqueue recorded instead of played, and return the pauses."""
    pauses = []
    player.enqueue = lambda audio, pause: pauses.append(pause) or True
    service = make_service(lambda unit: np.zeros(240, dtype=np.float32), player)
    service.speak(text)
    service.wait_idle(timeout=5.0)
    return pauses


def test_units_are_synthesised_in_order(player):
    seen = []
    def synth(text):
        seen.append(text)
        return np.zeros(240, dtype=np.float32)
    service = make_service(synth, player)
    service.speak("One thing. Two things.")
    service.wait_idle(timeout=5.0)
    assert seen == ["One thing.", "Two things."]


def test_pauses_come_from_split_reason(player):
    # Both shapes are pinned by the chunking tests, so this depends on no new policy:
    # two short sentences stay whole (SENTENCE -> 0.25), a leading clause is cut (CLAUSE -> 0.12).
    assert pauses_for(player, "One thing. Another thing.") == [0.25, 0.25]
    clause_first = pauses_for(player, "In short, this sentence runs long enough to be cut by the "
                                      "eighty character cap, and then it continues for a while.")
    assert clause_first[0] == 0.12


def test_second_speak_supersedes_the_first(player):
    started = threading.Event()
    release = threading.Event()
    def synth(text):
        if text.startswith("First"):
            started.set()
            release.wait(3.0)
        return np.zeros(240, dtype=np.float32)
    service = make_service(synth, player)
    service.speak("First sentence here now.")
    assert started.wait(3.0)
    service.speak("Second sentence here now.")
    release.set()
    service.wait_idle(timeout=5.0)
    played = service.played_units()
    assert not any(u.startswith("First") for u in played), "superseded text must never be heard"
    assert any(u.startswith("Second") for u in played)


def test_synthesis_error_is_reported_and_service_survives(player):
    errors = []
    def synth(text):
        raise ValueError("Nothing to synthesize")
    service = make_service(synth, player, on_error=errors.append)
    service.speak("Anything at all here.")
    service.wait_idle(timeout=5.0)
    assert errors and "Nothing to synthesize" in errors[0]
    assert service.status()["state"] == "IDLE"


def test_status_reports_generation_and_state(player):
    service = make_service(lambda text: np.zeros(240, dtype=np.float32), player)
    before = service.status()["generation"]
    service.speak("One sentence only.")
    service.wait_idle(timeout=5.0)
    assert service.status()["generation"] == before + 1
    assert set(service.status()) >= {"generation", "state", "voice", "speed", "paused"}


def test_currency_amount_is_never_split_across_units(player):
    seen = []
    service = make_service(lambda text: seen.append(text) or np.zeros(24, dtype=np.float32), player)
    service.speak("It costs $1,234.56 today, which is a great deal more than it used to be.")
    service.wait_idle(timeout=5.0)
    assert not any("$" in unit for unit in seen), "normalisation must run before chunking"
    assert any("dollars" in unit for unit in seen)
    assert not any(unit.strip().startswith("234") for unit in seen)


def test_unit_less_text_leaves_playback_alone(player):
    stops = []
    original_stop = player.stop
    player.stop = lambda: stops.append(True) or original_stop()
    service = make_service(lambda text: np.zeros(240, dtype=np.float32), player)
    before = service.status()["generation"]
    service.speak("   ")
    service.speak("...")
    assert stops == [], "a selection with nothing to say must not interrupt playback"
    assert service.status()["generation"] == before


def test_over_long_text_is_refused_to_the_caller(player):
    with pytest.raises(TooLongError):
        make_service(lambda text: np.zeros(240, dtype=np.float32), player).speak("word " * 5000)


def test_state_callback_reports_transitions_without_duplicates(player):
    states = []
    service = make_service(lambda text: np.zeros(240, dtype=np.float32), player,
                           on_state=states.append)
    service.speak("One thing.")
    service.wait_idle(timeout=5.0)
    assert states[0] == "PLAYING"
    assert states[-1] == "IDLE"
    assert all(a != b for a, b in zip(states, states[1:])), "no duplicate transitions"
