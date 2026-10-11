"""`utter doctor`: a diagnosis that explains a broken setup instead of failing on it.

The tests pin both halves of the contract. The report must carry the resolved config, the model
files, the chosen backend and the audio device *even when every one of them is missing* -- that
is the whole point of asking the doctor -- and the exit code must stay zero unless the machine
really cannot work as configured.
"""

import hashlib

import utter.doctor as doctor
from utter import cli, paths
from utter.config import Config
from utter.control import ControlServer
from utter.doctor import diagnose


def test_doctor_fails_when_models_are_missing(tmp_xdg):
    code, lines = diagnose(Config.load())
    assert code != 0
    assert any("kokoro-v1.0.int8.onnx" in line for line in lines)


def test_doctor_reports_the_resolved_config(tmp_xdg, monkeypatch):
    monkeypatch.setattr(doctor, "verify_models", lambda config: (True, "ok"))
    code, lines = diagnose(Config.load())
    assert code == 0
    assert any("voice" in line and "af_heart" in line for line in lines)
    assert any("hotkey backend" in line for line in lines)


def test_doctor_lists_the_audio_device(tmp_xdg, monkeypatch):
    monkeypatch.setattr(doctor, "verify_models", lambda config: (True, "ok"))
    _, lines = diagnose(Config.load())
    assert any("output device" in line for line in lines)


def test_doctor_reports_an_unavailable_audio_device_without_raising(tmp_xdg, monkeypatch):
    # `utter doctor` exists to explain a broken setup, so it must survive no device at all.
    monkeypatch.setattr(doctor, "verify_models", lambda config: (True, "ok"))
    monkeypatch.setattr(
        doctor.sd, "query_devices", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no device"))
    )
    code, lines = diagnose(Config.load())
    assert any("output device" in line for line in lines)
    assert code != 0, "a machine with no output device is a setup that cannot speak"


def test_doctor_reports_a_config_file_it_cannot_read(tmp_xdg, monkeypatch):
    # The caller already fell back to the defaults, so the report is the only place the user
    # can learn that the settings they wrote were ignored.
    monkeypatch.setattr(doctor, "verify_models", lambda config: (True, "ok"))
    path = paths.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not [ valid toml", encoding="utf-8")
    code, lines = diagnose(Config())
    assert any("unreadable" in line for line in lines)
    assert code != 0


def test_doctor_reports_the_last_five_latency_samples(tmp_xdg, monkeypatch):
    monkeypatch.setattr(doctor, "verify_models", lambda config: (True, "ok"))
    log = paths.log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "".join(
            f"2026-10-11 05:3{index}:00,000 INFO utter.tts.engine: utterance units=9 "
            f"audio=25.56 synth=6.9 ttfirst={value}\n"
            for index, value in enumerate(["0.9", "1.0", "2.0", "3.0", "4.0", "5.0", "6.0"])
        )
        + "2026-10-11 05:39:00,000 INFO utter.tts.engine: utterance units=0 audio=0.00 "
        "synth=0.00 ttfirst=-\n",
        encoding="utf-8",
    )
    _, lines = diagnose(Config.load())
    (latency,) = [line for line in lines if line.startswith("latency")]
    assert "2.0 3.0 4.0 5.0 6.0" in latency, "the five newest samples, oldest first"
    assert "0.9" not in latency and "1.0" not in latency, "older samples fall off the report"


def test_doctor_command_prints_the_report_and_returns_its_code(tmp_xdg, capsys):
    # The command has to work precisely when nothing is running -- it never asks an instance.
    assert cli.main(["doctor"]) == 1  # no model was installed under this XDG root
    out = capsys.readouterr().out
    assert "kokoro-v1.0.int8.onnx" in out
    assert "output device" in out
    assert "not running" in out


def test_doctor_command_survives_a_config_it_cannot_read(tmp_xdg, capsys):
    path = paths.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not [ valid toml", encoding="utf-8")
    assert cli.main(["doctor"]) == 1
    assert "unreadable" in capsys.readouterr().out


def test_doctor_verifies_a_pack_whose_files_match_their_digests(tmp_xdg):
    # The digest-match branch has to be the one that says so: a report that only ever showed
    # failures would be as useless as one that never showed them. Small stand-in files, because
    # what is being checked here is the check, not the model.
    models = paths.models_dir()
    models.mkdir(parents=True, exist_ok=True)
    config = Config()
    for name in (doctor.MODEL_NAME, doctor.VOICES_NAME):
        (models / name).write_bytes(f"contents of {name}".encode())
        digest = hashlib.sha256((models / name).read_bytes()).hexdigest()
        if name == doctor.MODEL_NAME:
            config.install.model_sha256 = digest
        else:
            config.install.voices_sha256 = digest

    code, lines = diagnose(config)
    assert code == 0
    assert sum("sha256 ok" in line for line in lines) == 2
    assert any("pack check:" in line and "ok" in line for line in lines)


def test_hotkey_show_prints_the_binding_steps(tmp_xdg, monkeypatch, capsys):
    # The steps are the hotkey module's own text, so the CLI and the tray notice cannot drift.
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert cli.main(["hotkey", "--show"]) == 0
    assert "utter speak" in capsys.readouterr().out


def test_hotkey_show_asks_a_running_instance_to_re_open_its_dialog(tmp_xdg, capsys):
    seen = []
    server = ControlServer(paths.socket_path(), {"hotkey": lambda request: seen.append(request) or {}})
    server.start()
    try:
        assert cli.main(["hotkey", "--show"]) == 0
    finally:
        server.stop()
    assert seen == [{"command": "hotkey", "show": True}]
    assert "utter speak" not in capsys.readouterr().out, "a dialog needs no manual steps"


def test_hotkey_show_prints_the_steps_when_the_instance_cannot_re_open_it(tmp_xdg, capsys):
    refusing = {"hotkey": lambda request: {"ok": False, "error": "no portal on this session"}}
    server = ControlServer(paths.socket_path(), refusing)
    server.start()
    try:
        assert cli.main(["hotkey", "--show"]) == 0
    finally:
        server.stop()
    assert "utter speak" in capsys.readouterr().out
