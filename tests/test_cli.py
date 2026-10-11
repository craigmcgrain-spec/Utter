"""cli.main: delegate to a running instance, and keep the cold fallback honest.

``FakeEngine`` stands in for the cold path, so these tests never build a model, a
session or an output device; the socket cases run against a ``ControlServer`` on the
temporary runtime directory the fixtures install.
"""

import wave

import numpy as np

from utter import cli, paths
from utter.control import ControlServer


class FakeEngine:
    """Stands in for the cold path: records what it was asked to say, lists a voice,
    and renders a WAV so the --save-wav wiring can be checked without a model."""

    def __init__(self, samples=None):
        self.spoken = []
        self.waits = []
        self._samples = (np.zeros(4800, dtype=np.float32) if samples is None
                         else np.asarray(samples, dtype=np.float32))

    def speak(self, text):
        self.spoken.append(text)

    def wait_idle(self, timeout):
        self.waits.append(timeout)
        return True

    def voice_names(self):
        return ["af_heart"]

    def render_to_wav(self, text, path):
        self.spoken.append(text)
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(24000)
            handle.writeframes(np.clip(self._samples, -1, 1).astype("<i2").tobytes())


def test_status_without_a_server_exits_non_zero(tmp_xdg, capsys):
    assert cli.main(["status"]) == 1
    assert "not running" in capsys.readouterr().err


def test_voices_lists_the_active_voice(tmp_xdg, monkeypatch, capsys):
    monkeypatch.setattr(cli, "build_engine", lambda **kw: FakeEngine())
    assert cli.main(["voices"]) == 0
    assert "af_heart" in capsys.readouterr().out


def test_speak_delegates_when_a_server_is_running(tmp_xdg):
    seen = []
    server = ControlServer(paths.socket_path(), {"speak": lambda req: seen.append(req["text"]) or {}})
    server.start()
    try:
        assert cli.main(["speak", "--text", "hello"]) == 0
        assert seen == ["hello"]
    finally:
        server.stop()


def test_speak_without_a_server_uses_the_cold_path(tmp_xdg, monkeypatch):
    used = []
    monkeypatch.setattr(cli, "build_engine", lambda **kw: used.append(True) or FakeEngine())
    assert cli.main(["speak", "--text", "hello"]) == 0
    assert used == [True]


def test_cold_speak_waits_for_the_utterance_to_finish(tmp_xdg, monkeypatch):
    engines = []

    def build(**kwargs):
        engine = FakeEngine()
        engines.append(engine)
        return engine

    monkeypatch.setattr(cli, "build_engine", build)
    assert cli.main(["speak", "--text", "hello"]) == 0
    assert cli.main(["speak", "--text", "long " * 400]) == 0
    short, long_selection = engines
    assert short.waits == [cli.COLD_SPEAK_WAIT_MIN], "a cold speak must outlive its own audio"
    assert long_selection.waits[0] > cli.COLD_SPEAK_WAIT_MIN, "a long selection waits longer"


def test_cold_speak_bound_covers_the_audio_not_just_the_synthesis(tmp_xdg, monkeypatch):
    # wait_idle returns once the last frame has been *played*, so the deadline has to outlast
    # the audio, not the synthesis: §2.3 measured 85.3 ms of audio per character for its
    # worst (number-dense) text. A bound below that expires while the utterance is playing.
    text = "word " * 400
    engine = FakeEngine()
    monkeypatch.setattr(cli, "build_engine", lambda **kw: engine)
    assert cli.main(["speak", "--text", text]) == 0
    (timeout,) = engine.waits
    assert timeout > len(text) * 0.0853, "the deadline must outlast the audio it waits for"


def test_cold_speak_reports_playback_that_never_finishes(tmp_xdg, monkeypatch, capsys):
    class StuckEngine(FakeEngine):
        def wait_idle(self, timeout):
            self.waits.append(timeout)
            return False

    monkeypatch.setattr(cli, "build_engine", lambda **kw: StuckEngine())
    assert cli.main(["speak", "--text", "hello"]) == 1
    assert "did not finish" in capsys.readouterr().err


def test_save_wav_writes_a_24000_hz_mono_file(tmp_xdg, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "build_engine",
                        lambda **kw: FakeEngine(samples=np.ones(4800, dtype=np.float32)))
    out = tmp_path / "out.wav"
    assert cli.main(["speak", "--text", "hello", "--save-wav", str(out)]) == 0
    with wave.open(str(out)) as handle:
        assert (handle.getframerate(), handle.getnchannels()) == (24000, 1)
