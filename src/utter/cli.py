"""Every user-facing command: delegate to the running tray, or fall back to a cold engine.

The tray process owns the warm model and the output device, so each command first tries the
control socket and only ``speak``, ``voices`` and ``doctor`` have anything to fall back to.
``status``, ``stop``, ``pause``, ``resume`` and ``hotkey`` are *only* meaningful against a
running instance: with nothing listening they report ``not running`` on stderr and exit
non-zero, because inventing an answer — or a second engine — would be worse than saying so.
A cold fallback is logged through :func:`utter.logging.setup_logging` as a cold start, which
is the one thing ``doctor`` and a support log need to see. A cold ``speak`` then *waits* for
its own playback (``SynthService.wait_idle``) before returning: the engine's worker and player
threads are daemons, so exiting as soon as the text is accepted would cut the utterance off
mid-sentence. That deadline is sized from the audio the engine is about to speak — the
phonemes the shipped pipeline counts for the real units, times the design's measured seconds
per phoneme — so it covers a long or digit-dense selection, and a wait that expires is
reported rather than waited on forever.

The commands this CLI sends, and the responses it reads (Task 13's handlers implement the
server side; the socket details live in :mod:`utter.control`):

``{"command": "speak", "text": str}``
    Saying the text. The running instance reads the current selection itself when ``text``
    is absent, which is what a desktop shortcut binding ``utter speak`` gets.
``{"command": "stop" | "pause" | "resume"}``
``{"command": "status"}``
    The five contract keys of ``SynthService.status()``; printed as JSON.
``{"command": "voices"}``
    ``{"voices": [str, ...], "active": str}`` — the pack's voices and the configured one.
``{"command": "doctor"}``
``{"command": "hotkey", "show": bool}``

``speak --save-wav`` is deliberately the one command that never delegates: it renders the text
locally through ``SynthService.render_to_wav`` and never plays anything. The design gives the
running instance no render-for-me handler, and a save must produce a file rather than depend
on — or disturb — whatever the tray is doing.

The cold engine is built by :func:`build_engine`, which the tests monkeypatch; ``play=False``
builds it without starting the player, so listing voices or rendering offline opens no output
device at all.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import capture, paths
from .config import Config, Speech
from .control import send
from .normalize import normalize
from .tts.audio import Player
from .tts.chunk import Unit, split_units
from .tts.engine import SynthService
from .tts.phonemes import EspeakPhonemizer, make_phonemizer

logger = logging.getLogger(__name__)

SECONDS_PER_PHONEME = 0.061
"""Seconds of audio per phoneme, the driver §2.3 established for how long a text speaks.

Its table measures 50-61 ms per phoneme across prose and number-dense text (13.65 s of audio
for 224 phonemes is the slowest row, and the value used here), so this is the pessimistic end
of the measured range rather than its middle.
"""

COLD_SPEAK_HEADROOM = 1.5
"""Slack over the audio a cold ``speak`` waits for: the deadline has to be reached only if
something is wrong, so it must not be the kind of bound a slow machine can trip by accident."""

PHONEMES_PER_CHAR = 7.0
"""Phonemes per character of spoken text, used **only** when counting them fails.

An upper bound, not an average: the densest text measured against this project's phonemiser is
``99¢`` (20 phonemes over its 3 characters, 6.7), with digit groups just behind (``5.5`` at 5.7,
``1500000`` at 5.0) and prose near 1.2. A cold start normally counts exactly; this matters only
when the phonemiser cannot run at all, where a deadline far longer than the audio is better
than a command that refuses to speak, and it is the reason a fallback still covers digit-dense
text instead of under-estimating it by a factor of three.
"""

COLD_SPEAK_WAIT_MIN = 60.0
"""Floor, in seconds, for how long a cold ``speak`` waits for its own playback.

The engine's worker and player threads are daemons, so returning as soon as the text is
accepted would let the process exit in the middle of the utterance and the user would hear a
fraction of a sentence. The wait is bounded rather than open-ended, so a wedged device or
synthesiser is reported instead of hanging the CLI.
"""


def build_engine(**kwargs) -> SynthService:
    """Build the cold engine: the real model store, player and service, once.

    ``config`` overrides the resolved configuration; ``play=False`` leaves the player
    unstarted so no output device is opened, which is what a voice listing and an offline
    render need. Everything the suite exercises goes through a monkeypatched ``build_engine``,
    so no test here loads a model or opens a device.
    """
    config = kwargs.pop("config", None) or _config()
    play = kwargs.pop("play", True)
    if kwargs:
        raise TypeError(f"build_engine() got unexpected keyword arguments {sorted(kwargs)}")
    # Imported inside the function: this is the only module in the package that imports
    # onnxruntime, and `utter status` must not pay for that on every invocation.
    from .tts.model import MODEL_NAME, VOICES_NAME, ModelStore

    store = ModelStore(
        paths.models_dir(),
        {
            MODEL_NAME: config.install.model_sha256,
            VOICES_NAME: config.install.voices_sha256,
        },
    )
    store.verify()  # fail before opening a device when the model is missing or mismatched
    player = Player()
    if play:
        try:
            player.start()
        except Exception as exc:  # sounddevice's own error type, and anything like it
            raise RuntimeError(f"cannot open the audio output: {exc}") from exc
    return SynthService(
        model=store,
        player=player,
        speech=config.speech,
        phonemizer=make_phonemizer(config.speech.phonemizer),
    )


def main(argv: list[str] | None = None) -> int:
    """Run one command and return the process exit status."""
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command is None:
            parser.error("a command is required")
    except SystemExit as exit_:  # argparse's own exits: --help and usage errors
        return int(exit_.code or 0)
    try:
        return _dispatch(args)
    except (OSError, ValueError, RuntimeError) as exc:
        # The boundary: an unreadable --file, an over-long selection, a missing model file or
        # a device that will not open are messages for the user, not tracebacks.
        return _fail(str(exc))


# -- commands --------------------------------------------------------------


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "speak":
        return _speak(args)
    if args.command == "status":
        return _status()
    if args.command == "voices":
        return _voices(as_json=args.json)
    if args.command == "doctor":
        return _doctor()
    if args.command == "hotkey":
        return _hotkey(show=args.show)
    return _control(args.command)  # stop, pause, resume


def _speak(args: argparse.Namespace) -> int:
    text = _speak_text(args)
    if text is None:
        return _fail("nothing to speak: give TEXT, --text or --file, or select some text")
    if args.save_wav is not None:
        return _render(text, Path(args.save_wav))
    response = send({"command": "speak", "text": text})
    if response is not None:
        return _report(response)
    return _cold_speak(text)


def _speak_text(args: argparse.Namespace) -> str | None:
    """The text to speak: ``--file``, then ``--text``, then TEXT, then the selection."""
    if args.file is not None:
        return Path(args.file).read_text(encoding="utf-8")
    if args.text_option is not None:
        return args.text_option
    if args.text is not None:
        return args.text
    selection = capture.read_selection(max_chars=_config().speech.max_chars)
    return selection.text if selection is not None else None


def _status() -> int:
    response = send({"command": "status"})
    if response is None:
        return _not_running()
    if not response.get("ok"):
        return _report(response)
    print(json.dumps(response))
    return 0


def _control(command: str) -> int:
    """``stop``/``pause``/``resume``: a live instance or nothing, never a cold engine."""
    response = send({"command": command})
    if response is None:
        return _not_running()
    return _report(response)


def _voices(as_json: bool) -> int:
    response = send({"command": "voices"})
    if response is not None:
        if not response.get("ok"):
            return _report(response)
        names, active = response.get("voices", []), response.get("active")
    else:
        config = _config()
        _cold_start(config, "voices")
        engine = build_engine(config=config, play=False)
        names, active = engine.voice_names(), config.speech.voice
    if as_json:
        print(json.dumps({"voices": names, "active": active}))
        return 0
    for name in names:
        print(f"{'*' if name == active else ' '} {name}")
    return 0


def _doctor() -> int:
    response = send({"command": "doctor"})
    if response is not None:
        if not response.get("ok"):
            return _report(response)
        print(json.dumps(response, indent=2, sort_keys=True))
        return 0
    # Nothing is running, so there is no engine to ask. This is the resolved configuration
    # and the paths it resolves to; the real body — audio device, hashes, latency samples —
    # is Task 16's. Every line comes off the disk or out of the config, so it cannot lie.
    config = _config()
    print("instance: not running")
    for label, path in (
        ("config", paths.config_path()),
        ("models", paths.models_dir()),
        ("log", paths.log_path()),
        ("socket", paths.socket_path()),
    ):
        state = "present" if path.exists() else "missing"
        print(f"{label + ':':<9} {path} ({state})")
    speech, hotkey, ui = config.speech, config.hotkey, config.ui
    print(f"speech:   voice={speech.voice} speed={speech.speed} lang={speech.lang} "
          f"phonemizer={speech.phonemizer} normalize={speech.normalize} "
          f"max_chars={speech.max_chars}")
    print(f"hotkey:   backend={hotkey.backend} speak={hotkey.speak} "
          f"stop={hotkey.stop} pause={hotkey.pause}")
    print(f"ui:       show_overlay={ui.show_overlay} overlay_pos={ui.overlay_pos}")
    return 0


def _hotkey(show: bool) -> int:
    """Re-open the portal binding dialog. The dialog itself is Task 16's."""
    response = send({"command": "hotkey", "show": show})
    if response is None:
        return _not_running()
    return _report(response)


# -- cold path -------------------------------------------------------------


def _cold_speak(text: str) -> int:
    """Speak with an engine of our own, for a machine with no tray running.

    The engine's threads are daemons, so this must outlive the audio it asked for: returning
    early would kill the worker and the player mid-utterance. ``wait_idle`` returns once the
    last frame has been written, and its bound is the text's own worst-case cost, so a stuck
    engine is reported rather than waited on forever.
    """
    config = _config()
    _cold_start(config, "speak")
    engine = build_engine(config=config)
    engine.speak(text)
    timeout = _speak_wait(text, config.speech)
    if not engine.wait_idle(timeout):
        return _fail(f"playback did not finish within {timeout:.0f}s")
    return 0


def _speak_wait(text: str, speech: Speech) -> float:
    """The deadline a cold ``speak`` gives its own playback, sized from the audio.

    ``wait_idle`` returns once the last frame has been *played*, so the deadline must outlast
    the audio, and §2.3 established phoneme count -- not character count -- as what drives its
    duration: digit-dense text speaks 3.6 phonemes per character where prose speaks 1.2, and a
    character budget therefore cannot cover it. The count comes from the real pipeline (the
    engine's normalise and split, the shipped phonemiser per unit), the per-unit trailing
    pause the player writes is counted too, and dividing by the configured speed covers the
    slowest supported one. It is a deadline, not a delay: a long selection really does have
    long audio, and one that expires is reported rather than waited on forever.
    """
    units = _spoken_units(text, speech)
    try:
        phonemes = _phoneme_count(units, speech)
    except Exception as exc:
        # A phonemiser that cannot run is a reason to be generous, not a reason to refuse to
        # speak: the command itself still goes through the engine and reports its own faults.
        logger.warning("cannot count phonemes (%s); sizing the cold wait by characters", exc)
        phonemes = int(sum(len(unit.text) for unit in units) * PHONEMES_PER_CHAR)
    audio = phonemes * SECONDS_PER_PHONEME / speech.speed
    return max(
        COLD_SPEAK_WAIT_MIN,
        audio * COLD_SPEAK_HEADROOM + len(units) * speech.pause_sentence,
    )


def _spoken_units(text: str, speech: Speech) -> list[Unit]:
    """The units the engine will speak for ``text``: the same normalise and split it runs."""
    prepared = normalize(text) if speech.normalize else text
    return split_units(prepared, max_chars=speech.max_chars)


def _phoneme_count(units: list[Unit], speech: Speech) -> int:
    """The phonemes the shipped phonemiser produces for ``units``, unit by unit."""
    phonemizer = EspeakPhonemizer()
    return sum(len(phonemizer.phonemize(unit.text, speech.lang)) for unit in units)


def _render(text: str, path: Path) -> int:
    """Render ``text`` to ``path`` and play nothing, whether or not a tray is running."""
    config = _config()
    _cold_start(config, "speak --save-wav")
    build_engine(config=config, play=False).render_to_wav(text, path)
    return 0


def _cold_start(config: Config, command: str) -> None:
    """Log that this invocation built its own engine instead of delegating (spec §8.3)."""
    from .logging import setup_logging  # lazy: onnxruntime is a cold-path cost, not a CLI one

    setup_logging(config.general.log_level)
    logger.info(
        "cold start: no instance owns %s, so %s built its own engine",
        paths.socket_path(),
        command,
    )


# -- plumbing --------------------------------------------------------------


def _config() -> Config:
    """The resolved configuration: the file, then the ``UTTER_*`` environment overrides."""
    return Config.load().merged_with_env()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="utter", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command")
    speak = commands.add_parser("speak", help="speak TEXT, or the current selection")
    speak.add_argument("text", nargs="?", help="the text to speak")
    speak.add_argument("--text", dest="text_option", metavar="TEXT", help="the text to speak")
    speak.add_argument("--file", metavar="PATH", help="read the text from PATH")
    speak.add_argument("--save-wav", dest="save_wav", metavar="PATH",
                       help="write PATH as a 24 kHz mono WAV instead of playing anything")
    commands.add_parser("status", help="print the running instance's status as JSON")
    commands.add_parser("stop", help="stop speaking")
    commands.add_parser("pause", help="pause playback")
    commands.add_parser("resume", help="resume playback")
    voices = commands.add_parser("voices", help="list the voices in the model pack")
    voices.add_argument("--json", action="store_true", help="print the list as JSON")
    commands.add_parser("doctor", help="report the resolved config and the model paths")
    hotkey = commands.add_parser("hotkey", help="re-open the hotkey binding dialog")
    hotkey.add_argument("--show", action="store_true", help="show the binding dialog")
    return parser


def _report(response: dict) -> int:
    """Turn a response object into an exit status, printing the error it carries."""
    if response.get("ok"):
        return 0
    return _fail(response.get("error", "the command failed"))


def _not_running() -> int:
    return _fail(f"not running: no utter instance owns {paths.socket_path()}")


def _fail(message: str) -> int:
    print(f"utter: {message}", file=sys.stderr)
    return 1
