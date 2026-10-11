"""`utter doctor`: a diagnosis that explains a broken setup instead of failing on it.

The tests pin both halves of the contract. The report must carry the resolved config, the model
files, the chosen backend and the audio device *even when every one of them is missing* -- that
is the whole point of asking the doctor -- and the exit code must stay zero unless the machine
really cannot work as configured.
"""

import hashlib
import threading

import utter.doctor as doctor
from utter import cli, paths
from utter.config import Config
from utter.control import ControlServer
from utter.doctor import diagnose
from utter.hotkey import PortalError
from utter.ui import app


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


class _RecordingBackend:
    """A hotkey backend that records which thread started it, and stops when told.

    ``bound`` is the portal's own record of the ids the desktop confirmed; ``call_unbound`` makes
    a start conclude with nothing bound straight away, which is what a refused bind reports.
    """

    def __init__(self, bound=("speak", "stop"), call_unbound=False):
        self.bound = list(bound)
        self.declined = False
        self.starts: list[threading.Thread] = []
        self.stops = 0
        self._call_unbound = call_unbound

    def start(self, on_action, on_unbound=None):
        self.starts.append(threading.current_thread())
        if self._call_unbound and on_unbound is not None:
            on_unbound()

    def stop(self):
        self.stops += 1


def _serve(fake_service, config, monkeypatch, backend):
    """Serve the app's real handler table with ``backend`` making every bind it is asked for."""
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/utter-test-bus")
    monkeypatch.setattr(app, "make_backend", lambda name, cfg: backend)
    # A sibling module's configure_hotkey test leaves a backend behind; this one must see its own.
    monkeypatch.setattr(app, "_backend", None)
    monkeypatch.setattr(app, "_unbound_reported", False)
    server = ControlServer(paths.socket_path(), app.served_handlers(fake_service, config))
    server.start()
    return server


def _pumping_cli(qapp, argv) -> int:
    """Run ``cli.main`` off the GUI thread while the GUI thread pumps the queued work."""
    result = {}

    def run_cli():
        result["code"] = cli.main(argv)

    worker = threading.Thread(target=run_cli)
    worker.start()
    try:
        for _ in range(1000):  # let the queued rebind reach the GUI thread and be answered
            qapp.processEvents()
            if not worker.is_alive():
                break
        assert not worker.is_alive(), "the command must answer rather than hang"
    finally:
        worker.join(timeout=10)
    return result["code"]


def test_hotkey_show_re_issues_the_bind_on_the_gui_thread(
    qapp, tmp_xdg, fake_service, config, monkeypatch, capsys
):
    # The dialog is the desktop's and the bind builds Qt objects, so the socket thread hands the
    # rebind to the GUI thread -- exactly as a text-less `speak` hands over its selection read --
    # and the answer carries the ids the desktop confirmed rather than a guess from the session
    # type, which is what used to send a user to bind keys that were already bound.
    backend = _RecordingBackend()
    server = _serve(fake_service, config, monkeypatch, backend)
    try:
        assert _pumping_cli(qapp, ["hotkey", "--show"]) == 0
    finally:
        server.stop()

    assert backend.starts == [threading.main_thread()], "exactly one bind, on the GUI thread"
    assert backend.stops == 0, "the re-issued bind stays in place"
    out = capsys.readouterr().out
    assert "speak" in out and "stop" in out
    assert "Settings -> Keyboard" not in out, "a bind that worked needs no manual steps"


def test_hotkey_show_answers_the_manual_steps_when_nothing_can_bind(
    qapp, tmp_xdg, fake_service, config, monkeypatch, capsys
):
    # No session can bind these: the answer is the hotkey module's own text, the same string the
    # tray notice carries, and the exit status is still 0 -- this is help, not a failure.
    monkeypatch.setattr(app, "make_backend", lambda name, cfg: None)
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr(app, "_backend", None)
    monkeypatch.setattr(app, "_unbound_reported", False)
    server = ControlServer(paths.socket_path(), app.served_handlers(fake_service, config))
    server.start()
    try:
        assert _pumping_cli(qapp, ["hotkey", "--show"]) == 0
    finally:
        server.stop()
    assert "utter speak" in capsys.readouterr().out


def test_hotkey_show_answers_the_manual_steps_when_the_bind_concludes_unbound(
    qapp, tmp_xdg, fake_service, config, monkeypatch, capsys
):
    # A bind that the desktop refused reports it on the backend's own thread, and that verdict --
    # not the session type -- is what turns the answer into the manual steps.
    backend = _RecordingBackend(bound=(), call_unbound=True)
    server = _serve(fake_service, config, monkeypatch, backend)
    try:
        assert _pumping_cli(qapp, ["hotkey", "--show"]) == 0
    finally:
        server.stop()
    assert "utter speak" in capsys.readouterr().out


def test_hotkey_show_answers_when_the_rebind_itself_fails(
    qapp, tmp_xdg, fake_service, config, monkeypatch, capsys
):
    # A bind that raises has to come back as an answer: an exception escaping a Qt slot replies to
    # nobody, and the client is waiting on the connection.
    class Exploding(_RecordingBackend):
        def start(self, on_action, on_unbound=None):
            raise PortalError("the portal is gone")

    server = _serve(fake_service, config, monkeypatch, Exploding())
    try:
        assert _pumping_cli(qapp, ["hotkey", "--show"]) == 0
    finally:
        server.stop()
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
