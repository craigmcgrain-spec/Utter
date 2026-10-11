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
        self._samples = (np.zeros(4800, dtype=np.float32) if samples is None
                         else np.asarray(samples, dtype=np.float32))

    def speak(self, text):
        self.spoken.append(text)

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


def test_save_wav_writes_a_24000_hz_mono_file(tmp_xdg, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "build_engine",
                        lambda **kw: FakeEngine(samples=np.ones(4800, dtype=np.float32)))
    out = tmp_path / "out.wav"
    assert cli.main(["speak", "--text", "hello", "--save-wav", str(out)]) == 0
    with wave.open(str(out)) as handle:
        assert (handle.getframerate(), handle.getnchannels()) == (24000, 1)
