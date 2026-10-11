"""The composition root: the control handlers, the GUI-thread bridge and the hotkey wiring.

A reader who only checked that ``run`` returns would miss what a plausible implementation
gets wrong: a handler that lets ``TooLongError`` escape into the socket server, a state
callback that runs a widget from the engine's worker thread, a "no hotkey" notice keyed on
the backend's *name* rather than on nothing being bound (a declined dialog and a dead bus
both name a real backend), a selection read with capture's own 20000 default instead of the
configured limit, and a second instance that starts a second tray instead of delegating.

Two more are invisible from a single call: a rebind that builds itself another
:class:`~utter.ui.app._HotkeyBridge` instead of reusing the one the process already has (the
application then accumulates one QObject per ``utter hotkey --show``), and the two control
surfaces each holding their own idea of the voice and the speed instead of sharing them.
"""

import threading
import time

import numpy as np

import utter.ui.app as app
import utter.ui.overlay as overlay_module
from utter import control, paths
from utter.capture import Selection
from utter.config import Config
from utter.control import ControlServer, send
from utter.tts.chunk import TooLongError
from utter.ui.app import build_handlers, build_state_bridge, configure_hotkey, delegate_if_running


def _pump_until(qapp, predicate, timeout=2.0):
    """Pump the event loop until ``predicate`` holds, or the timeout expires; report which."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    qapp.processEvents()
    return predicate()


def _submenu(menu, prefix):
    return next(a.menu() for a in menu.actions() if a.menu() and a.text().startswith(prefix))


def _voice_items(menu):
    voice_menu = _submenu(menu, "Voice")
    return [item for language in voice_menu.findChildren(type(voice_menu)) for item in language.actions()]


def _speed_items(menu):
    return _submenu(menu, "Speed").actions()


def test_the_selection_deadline_sits_below_the_client_deadline():
    # A client that gives up on the read is told nothing answered, which `utter speak` reads as
    # "not running" and answers by cold-starting a second engine -- the text spoken twice. The
    # reader must therefore always give up first, and this pins the relationship in one place.
    assert app.SELECTION_TIMEOUT < control.TIMEOUT


def test_handlers_cover_the_control_protocol(fake_service, config):
    handlers = build_handlers(fake_service, config)
    assert set(handlers) >= {"speak", "stop", "pause", "resume", "status"}


def test_speak_handler_reads_the_selection_when_no_text_is_given(fake_service, config, monkeypatch):
    seen = {}

    def recording(**kw):
        seen.update(kw)
        return Selection("selected words", "primary")

    monkeypatch.setattr(app, "read_selection", recording)
    config.speech.max_chars = 4242
    assert build_handlers(fake_service, config)["speak"]({})["ok"] is True
    assert fake_service.spoken == ["selected words"]
    assert seen.get("max_chars") == 4242


def test_speak_handler_prefers_explicit_text(fake_service, config, monkeypatch):
    monkeypatch.setattr(app, "read_selection", lambda **kw: Selection("ignored", "primary"))
    build_handlers(fake_service, config)["speak"]({"text": "from the socket"})
    assert fake_service.spoken == ["from the socket"]


def test_speak_handler_answers_a_refusal_without_raising(fake_service, config, monkeypatch):
    def refuse(text):
        raise TooLongError("selection is over the limit")

    fake_service.speak = refuse
    response = build_handlers(fake_service, config)["speak"]({"text": "anything"})
    assert response["ok"] is False and "over the limit" in response["error"]


def test_speak_handler_answers_a_missing_selection_without_raising(fake_service, config, monkeypatch):
    monkeypatch.setattr(app, "read_selection", lambda **kw: None)
    response = build_handlers(fake_service, config)["speak"]({})
    assert response["ok"] is False and "text" in response["error"].lower()


def test_status_handler_reports_service_status(fake_service, config):
    response = build_handlers(fake_service, config)["status"]({})
    assert response["ok"] is True and "state" in response


def test_state_bridge_delivers_on_the_gui_thread(qapp):
    # SynthService.on_state may fire on the worker thread, so the app marshals it: a slot
    # connected to the bridge must run on the GUI thread, never on the emitter's.
    bridge = build_state_bridge()
    seen = []
    bridge.changed.connect(lambda state: seen.append((state, threading.current_thread())))
    worker = threading.Thread(target=lambda: bridge.changed.emit("PLAYING"))
    worker.start()
    worker.join()
    for _ in range(50):  # let the queued connection be delivered
        qapp.processEvents()
        if seen:
            break
    assert seen, "the bridge must deliver the state to the GUI thread"
    assert seen[0][0] == "PLAYING"
    assert seen[0][1] is threading.main_thread()


def test_no_hotkey_backend_notifies_with_binding_instructions(fake_service, config, monkeypatch):
    notices = []
    monkeypatch.setattr(app, "notify", lambda *args, **kwargs: notices.append(args))
    monkeypatch.setattr(app, "make_backend", lambda name, cfg: None)
    backend = configure_hotkey(config, fake_service, session_type="wayland",
                               portal_available=False, notifier=None)
    assert backend == "none"
    assert notices and "utter speak" in str(notices[0])


def test_a_bind_that_concludes_empty_notifies_later(qapp, fake_service, config, monkeypatch):
    # The portal's bind is asynchronous -- the desktop answers a dialog whenever the user gets
    # to it -- so `bound` is still empty when start() returns even on a bind that is about to
    # succeed. The notice must wait for the backend to report the outcome, and that report
    # arrives on the backend's thread, so it reaches the tray as a queued GUI-thread call.
    notices = []
    monkeypatch.setattr(
        app, "notify",
        lambda *args, **kwargs: notices.append((args, threading.current_thread())),
    )

    class BindingLater:
        bound = []

        def start(self, on_action, on_unbound=None):
            self.on_unbound = on_unbound

        def stop(self):
            pass

    backend = BindingLater()
    monkeypatch.setattr(app, "make_backend", lambda name, cfg: backend)
    assert configure_hotkey(config, fake_service, session_type="wayland",
                            portal_available=True, notifier=None) == "portal"
    assert notices == [], "a bind still in progress is not an unbound bind"

    worker = threading.Thread(target=backend.on_unbound)
    worker.start()
    worker.join()
    for _ in range(50):  # let the queued connection be delivered
        qapp.processEvents()
        if notices:
            break
    assert len(notices) == 1, "exactly one notice, and only once the bind concluded"
    assert "utter speak" in str(notices[0][0])
    assert notices[0][1] is threading.main_thread(), (
        "the backend reports from its own thread, so the notice must be a queued GUI call"
    )


def test_a_successful_bind_notifies_nothing(fake_service, config, monkeypatch):
    notices = []
    monkeypatch.setattr(app, "notify", lambda *args, **kwargs: notices.append(args))

    class Bound:
        bound = ["speak"]

        def start(self, on_action, on_unbound=None):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(app, "make_backend", lambda name, cfg: Bound())
    assert configure_hotkey(config, fake_service, session_type="wayland",
                            portal_available=True, notifier=None) == "portal"
    assert notices == [], "a bind that succeeded needs no manual instructions"


def test_an_engine_error_is_delivered_on_the_gui_thread(qapp, fake_service, config, monkeypatch):
    # SynthService.on_error is reached from the `utter-synth` worker too, so a failure must
    # reach the tray the same way a state change does: the balloon is a widget call, and the
    # app must not make it off the GUI thread.
    notices = []
    monkeypatch.setattr(
        app, "notify",
        lambda *args, **kwargs: notices.append((args, threading.current_thread())),
    )
    bridge = build_state_bridge()
    tray = app.Tray(fake_service, config, on_quit=lambda: None)
    overlay = app.Overlay(fake_service, config)
    app._connect_engine(bridge, config, tray, overlay)

    worker = threading.Thread(target=lambda: bridge.error.emit("the synthesiser fell over"))
    worker.start()
    worker.join()
    for _ in range(50):  # let the queued connection be delivered
        qapp.processEvents()
        if notices:
            break
    assert len(notices) == 1, "the engine reports one fault, so the user sees one notice"
    assert notices[0][0][1] == "the synthesiser fell over"
    assert notices[0][1] is threading.main_thread(), (
        "the error callback runs on the worker thread, so delivery must be queued"
    )


def test_a_lost_audio_device_is_reported_on_the_gui_thread(qapp, fake_service, config, monkeypatch):
    # The player's on_error fires on its own writer thread, so a mid-playback device loss that
    # reached the tray directly would be a widget call off the GUI thread -- and if nothing
    # were wired at all, the user would simply hear the audio stop with no explanation.
    from conftest import FakeStream
    from utter.tts.audio import Player

    notices = []
    monkeypatch.setattr(
        app, "notify",
        lambda *args, **kwargs: notices.append((args, threading.current_thread())),
    )
    bridge = build_state_bridge()
    tray = app.Tray(fake_service, config, on_quit=lambda: None)
    overlay = app.Overlay(fake_service, config)
    app._connect_engine(bridge, config, tray, overlay)

    class BrokenStream(FakeStream):
        def write(self, data):
            raise RuntimeError("device gone")

    player = Player(stream_factory=BrokenStream, on_error=app._audio_error(bridge))
    player.start()
    try:
        player.enqueue(np.ones(2400, dtype=np.float32), pause=0.0)
        assert player.wait_idle(2.0)
        for _ in range(50):  # let the queued connection be delivered
            qapp.processEvents()
            if notices:
                break
    finally:
        player.shutdown()
        del tray, bridge  # outlive the writer thread, as run() holds them for the session
    assert len(notices) == 1
    assert notices[0][0][1] == "Audio device stopped: device gone"
    assert notices[0][1] is threading.main_thread(), (
        "the write failure is on the player's writer thread, so delivery must be queued"
    )


def test_a_text_less_speak_reads_the_selection_on_the_gui_thread(qapp, fake_service, config, monkeypatch):
    # `speak` without `text` runs on a control server's connection thread, but the read ends in
    # QApplication.clipboard() on X11. The handler must therefore ask the GUI thread and wait.
    seen = []

    def recording(**kwargs):
        seen.append(threading.current_thread())
        return Selection("selected words", "primary")

    monkeypatch.setattr(app, "read_selection", recording)
    reader = app._GuiSelectionReader()
    handlers = build_handlers(
        fake_service, config,
        read=lambda max_chars: reader.selection(max_chars=max_chars, timeout=5.0),
    )

    response = {}

    def ask():
        response.update(handlers["speak"]({}))

    worker = threading.Thread(target=ask)
    worker.start()
    for _ in range(200):  # pump the GUI thread until the asker is answered
        qapp.processEvents()
        if not worker.is_alive():
            break
    worker.join(timeout=5)

    assert response["ok"] is True, response
    assert fake_service.spoken == ["selected words"]
    assert seen and seen[0] is threading.main_thread(), (
        "the selection read belongs on the GUI thread, not on the socket's"
    )


def test_the_served_handlers_read_the_selection_on_the_gui_thread(qapp, fake_service, config, monkeypatch, tmp_xdg):
    # The table run() actually serves, driven through the real socket: the pin for run()'s
    # wiring, so a caller that forgot the GUI reader fails here rather than only on X11.
    seen = []

    def recording(**kwargs):
        seen.append(threading.current_thread())
        return Selection("selected words", "primary")

    monkeypatch.setattr(app, "read_selection", recording)
    server = ControlServer(paths.socket_path(), app.served_handlers(fake_service, config))
    server.start()

    response = {}

    def ask():
        response.update(send({"command": "speak"}) or {})

    worker = threading.Thread(target=ask)
    worker.start()
    try:
        for _ in range(500):  # pump the GUI thread until the socket answers
            qapp.processEvents()
            if not worker.is_alive():
                break
        worker.join(timeout=5)
    finally:
        server.stop()

    assert response.get("ok") is True, response
    assert fake_service.spoken == ["selected words"]
    assert seen and seen[0] is threading.main_thread(), (
        "served_handlers must put the selection read on the GUI thread"
    )


def test_a_text_less_speak_that_times_out_answers_with_an_error(qapp, config, fake_service, monkeypatch):
    # A GUI thread that never pumps must produce an error response, not a hung connection.
    monkeypatch.setattr(app, "read_selection", lambda **kw: Selection("never read", "primary"))
    reader = app._GuiSelectionReader()
    handlers = build_handlers(
        fake_service, config,
        read=lambda max_chars: reader.selection(max_chars=max_chars, timeout=0.05),
    )

    response = {}

    def ask():
        response.update(handlers["speak"]({}))

    worker = threading.Thread(target=ask)   # nothing pumps the GUI thread while it waits
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive(), "a read that never returns must not hang the connection"
    assert response["ok"] is False and "could not read the selection" in response["error"]
    assert fake_service.spoken == []
    for _ in range(10):  # drain the read left queued, so no event outlives this test
        qapp.processEvents()


def test_the_overlay_speed_is_applied_and_persisted(qapp, fake_service, config, tmp_xdg):
    # The overlay's slider is the only control that reaches the engine through Config, so the
    # wiring is what makes it audible at all -- and it must survive a restart.
    bridge = build_state_bridge()
    tray = app.Tray(fake_service, config, on_quit=lambda: None)
    overlay = app.Overlay(fake_service, config)
    app._connect_engine(bridge, config, tray, overlay)

    overlay.speed_slider.setValue(150)  # not a drag: commits at once

    assert config.speech.speed == 1.5, "the engine reads this field from the next unit"
    assert Config.load().speech.speed == 1.5, "a committed speed must survive a restart"


def test_the_overlay_speed_moves_the_menu_check(qapp, fake_service, config, tmp_xdg):
    # Both surfaces show the same speed, so committing one from the slider has to move the
    # menu's check as well -- including off a preset, where the old menu showed nothing.
    bridge = build_state_bridge()
    tray = app.Tray(fake_service, config, on_quit=lambda: None)
    overlay = app.Overlay(fake_service, config)
    app._connect_engine(bridge, config, tray, overlay)
    menu = tray.contextMenu()

    overlay.speed_slider.setValue(150)
    assert [a.text() for a in _speed_items(menu) if a.isChecked()] == ["1.5×"]

    overlay.speed_slider.setValue(137)
    assert [a.text() for a in _speed_items(menu) if a.isChecked()] == ["Custom (1.37×)"]
    assert config.speech.speed == 1.37


def test_a_menu_speed_moves_the_overlay_slider(qapp, fake_service, config, tmp_xdg):
    # The other direction of the same contract: a Speed choice must be visible on the slider
    # the user can see, not only in the menu they just closed.
    bridge = build_state_bridge()
    tray = app.Tray(fake_service, config, on_quit=lambda: None)
    overlay = app.Overlay(fake_service, config)
    app._connect_engine(bridge, config, tray, overlay)

    next(a for a in _speed_items(tray.contextMenu()) if a.text() == "1.25×").trigger()

    assert overlay.speed_slider.value() == 125
    assert overlay.speed_label.text() == "1.25×"
    assert config.speech.speed == 1.25


def test_a_menu_voice_choice_updates_the_overlay_label(qapp, fake_service, config, tmp_xdg):
    # The overlay used to be labelled once, at construction, and kept showing the voice the
    # app started with however many times the tray changed it.
    bridge = build_state_bridge()
    tray = app.Tray(fake_service, config, on_quit=lambda: None)
    overlay = app.Overlay(fake_service, config)
    app._connect_engine(bridge, config, tray, overlay)

    item = next(a for a in _voice_items(tray.contextMenu()) if a.data() == "am_michael")
    item.trigger()

    assert config.speech.voice == "am_michael"
    assert overlay.voice_label.text() == item.text().split(" (")[0]


def test_a_dragged_overlay_is_remembered_on_x11(qapp, fake_service, config, tmp_xdg, monkeypatch):
    # Nothing ever wrote overlay_pos, so the restored position was always the default. The
    # settled move must reach the config file through the tray's own persist helper.
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setattr(overlay_module, "_POSITION_SETTLE_MS", 20)
    bridge = build_state_bridge()
    tray = app.Tray(fake_service, config, on_quit=lambda: None)
    overlay = app.Overlay(fake_service, config)
    app._connect_engine(bridge, config, tray, overlay)
    overlay.show_for_state("PLAYING")  # a hidden widget is not where a drag can move it

    overlay.move(120, 240)
    assert _pump_until(qapp, lambda: config.ui.overlay_pos == (120, 240))
    assert Config.load().ui.overlay_pos == (120, 240), "the position must survive a restart"


def test_rebinding_keeps_one_bridge_and_dispatches_each_action_once(
    qapp, fake_service, config, monkeypatch
):
    # Every `utter hotkey --show` used to build another bridge parented to the application and
    # never release it; each one also stayed wired to its handler, which is how a single
    # keypress could be acted on more than once.
    class Backend:
        bound = []

        def start(self, on_action, on_unbound=None):
            self.on_action = on_action

        def stop(self):
            pass

    monkeypatch.setattr(app, "make_backend", lambda name, cfg: Backend())
    for _ in range(5):
        app.configure_hotkey(
            config,
            fake_service,
            session_type="wayland",
            portal_available=True,
            notifier=None,
        )

    bridges = [child for child in qapp.children() if isinstance(child, app._HotkeyBridge)]
    assert len(bridges) == 1, "five rebinds must leave exactly one bridge"

    fake_service.calls.clear()
    bridges[0].action.emit("stop")
    assert fake_service.calls == ["stop"], "one emitted action is dispatched exactly once"


def test_a_hotkey_speak_action_reads_the_selection_with_the_speech_limit(
    qapp, fake_service, config, monkeypatch
):
    # The dispatch is the same handler the socket serves, and it needs the *speech* settings to
    # read the selection. Handing the bind only the hotkey table made the read raise
    # AttributeError inside a Qt slot, which PyQt answers by aborting the process.
    seen = {}

    def recording(**kwargs):
        seen.update(kwargs)
        return Selection("chosen by hotkey", "primary")

    class Backend:
        bound = []

        def start(self, on_action, on_unbound=None):
            self.on_action = on_action

        def stop(self):
            pass

    monkeypatch.setattr(app, "make_backend", lambda name, cfg: Backend())
    monkeypatch.setattr(app, "read_selection", recording)
    config.speech.max_chars = 4321
    configure_hotkey(config, fake_service, session_type="wayland",
                     portal_available=True, notifier=None)

    bridge = next(child for child in qapp.children() if isinstance(child, app._HotkeyBridge))
    bridge.action.emit("speak")

    assert fake_service.spoken == ["chosen by hotkey"]
    assert seen.get("max_chars") == 4321


def test_a_hotkey_action_reaches_the_bind_that_is_current(qapp, config, monkeypatch):
    # The one bridge is connected once, so the handlers have to read the *current* service
    # rather than the one that happened to be passed to the first bind.
    from conftest import FakeService

    class Backend:
        bound = []

        def start(self, on_action, on_unbound=None):
            self.on_action = on_action

        def stop(self):
            pass

    monkeypatch.setattr(app, "make_backend", lambda name, cfg: Backend())
    first, second = FakeService(), FakeService()
    app.configure_hotkey(config, first, session_type="wayland",
                         portal_available=True, notifier=None)
    app.configure_hotkey(config, second, session_type="wayland",
                         portal_available=True, notifier=None)

    bridge = next(child for child in qapp.children() if isinstance(child, app._HotkeyBridge))
    bridge.action.emit("stop")

    assert second.calls == ["stop"]
    assert first.calls == [], "the replaced bind's service must no longer be driven"


def test_second_instance_delegates(tmp_xdg):
    server = ControlServer(paths.socket_path(), {"show": lambda req: {}})
    server.start()
    try:
        assert delegate_if_running() is True
    finally:
        server.stop()


def test_a_lost_socket_race_is_answered_as_a_delegation(tmp_xdg, capsys, monkeypatch):
    # The probe said no instance was serving, and then one bound in between: the exception is the
    # same delegation the opening probe makes, and the answer addresses that instance.
    class Taken:
        def start(self):
            raise RuntimeError("another utter instance already owns the socket")

    monkeypatch.setattr(app, "delegate_if_running", lambda: True)

    assert app._start_server(Taken()) == 0
    assert capsys.readouterr().err == ""


def test_a_socket_that_cannot_be_served_is_not_reported_as_a_delegation(tmp_xdg, capsys, monkeypatch):
    # start() raises the same RuntimeError for a path that cannot be served at all -- an
    # unwritable runtime directory, a path something else holds. Answering 0 for that exits
    # successfully with no tray and nothing said, so the verdict is evidence: something either
    # answers `show` on the socket, or this is a failure worth a message and a non-zero status.
    class Unservable:
        def start(self):
            raise RuntimeError(f"cannot serve on {paths.socket_path()}: Permission denied")

    monkeypatch.setattr(app, "delegate_if_running", lambda: False)

    assert app._start_server(Unservable()) == 1
    err = capsys.readouterr().err
    assert "Permission denied" in err and str(paths.socket_path()) in err


def test_a_socket_that_starts_needs_no_verdict(tmp_xdg):
    class Serving:
        started = False

        def start(self):
            self.started = True

    server = Serving()
    assert app._start_server(server) is None and server.started


def test_a_missing_model_is_reported_before_qt_is_built(tmp_xdg, capsys, monkeypatch):
    # The module's contract is that the model is verified before Qt is built at all: a broken
    # install has to be a one-line message and an exit status whether or not this machine has a
    # display server a tray could be built on, rather than a Qt that cannot start reporting it.
    class Missing:
        def __init__(self, *args, **kwargs):
            pass

        def verify(self):
            raise app.ModelError(f"model file not found: {paths.models_dir()}")

    def refuse_qt(*args, **kwargs):
        raise AssertionError("QApplication must not be built before the model is verified")

    monkeypatch.setattr(app, "ModelStore", Missing)
    monkeypatch.setattr(app, "QApplication", refuse_qt)

    assert app.run([]) == 1
    err = capsys.readouterr().err
    assert "model file not found" in err and "doctor" in err


def test_an_unreadable_voice_pack_is_reported_before_qt_is_built(tmp_xdg, capsys, monkeypatch):
    # With no digest recorded, verify() only sees that the files exist; the tray build is the
    # first thing that opens the voice pack, and it must not be what finds it unreadable.
    models = paths.models_dir()
    models.mkdir(parents=True)
    (models / app.MODEL_NAME).write_bytes(b"model")
    (models / app.VOICES_NAME).write_bytes(b"not an npz archive")

    def refuse_qt(*args, **kwargs):
        raise AssertionError("QApplication must not be built before the pack is read")

    monkeypatch.setattr(app, "QApplication", refuse_qt)

    assert app.run([]) == 1
    err = capsys.readouterr().err
    assert "voice pack" in err and "doctor" in err


def test_a_config_the_tray_cannot_read_is_reported_instead_of_crashing(tmp_xdg, capsys):
    # The tray is started from a desktop file with no terminal, so a hand-edited config.toml
    # has to come back as a message and an exit status: `cli.main` catches the same error at its
    # boundary and `utter doctor` prints it as a line, and a traceback out of the startup path
    # leaves the user with nothing at all.
    path = paths.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not [ valid toml", encoding="utf-8")

    assert app.run([]) == 1
    err = capsys.readouterr().err
    assert err.startswith("utter:") and "doctor" in err


def test_an_unwritable_log_directory_is_reported_but_not_fatal(tmp_xdg, capsys, monkeypatch):
    # A state directory the process cannot write is a diagnostic the app can live without: the
    # log is how a support report is read, not how the app speaks, so the failure is printed and
    # startup continues rather than dying over a file nothing needs in order to speak.
    def refuse(level):
        raise OSError("read-only file system")

    monkeypatch.setattr(app, "setup_logging", refuse)
    app._setup_logging("info")

    err = capsys.readouterr().err
    assert "read-only file system" in err and str(paths.log_path()) in err
