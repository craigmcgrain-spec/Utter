"""The composition root: the control handlers, the GUI-thread bridge and the hotkey wiring.

A reader who only checked that ``run`` returns would miss what a plausible implementation
gets wrong: a handler that lets ``TooLongError`` escape into the socket server, a state
callback that runs a widget from the engine's worker thread, a "no hotkey" notice keyed on
the backend's *name* rather than on nothing being bound (a declined dialog and a dead bus
both name a real backend), a selection read with capture's own 20000 default instead of the
configured limit, and a second instance that starts a second tray instead of delegating.
"""

import threading

import utter.ui.app as app
from utter import control, paths
from utter.capture import Selection
from utter.config import Config
from utter.control import ControlServer, send
from utter.tts.chunk import TooLongError
from utter.ui.app import build_handlers, build_state_bridge, configure_hotkey, delegate_if_running


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
    backend = configure_hotkey(config.hotkey, fake_service, session_type="wayland",
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
    assert configure_hotkey(config.hotkey, fake_service, session_type="wayland",
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
    assert configure_hotkey(config.hotkey, fake_service, session_type="wayland",
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


def test_second_instance_delegates(tmp_xdg):
    server = ControlServer(paths.socket_path(), {"show": lambda req: {}})
    server.start()
    try:
        assert delegate_if_running() is True
    finally:
        server.stop()
