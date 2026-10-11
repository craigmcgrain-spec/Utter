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
from utter import paths
from utter.capture import Selection
from utter.config import Config
from utter.control import ControlServer
from utter.tts.chunk import TooLongError
from utter.ui.app import build_handlers, build_state_bridge, configure_hotkey, delegate_if_running


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
    monkeypatch.setattr(app, "notify", lambda *args, **kwargs: notices.append(args))

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
    assert "utter speak" in str(notices[0])


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


def test_second_instance_delegates(tmp_xdg):
    server = ControlServer(paths.socket_path(), {"show": lambda req: {}})
    server.start()
    try:
        assert delegate_if_running() is True
    finally:
        server.stop()
