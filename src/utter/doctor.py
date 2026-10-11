"""``utter doctor``: explain a broken setup in lines, and never fail on the way.

The app ships no download code, so when it cannot speak the only useful answer is a report of
what is on the machine: the resolved configuration, every model file with the digest the
installer recorded for it and the result of checking that digest, the hotkey backend the session
selects with the portal app-id it registers, the default output device, and the last few
time-to-first-audio samples from the log. Every input is read off the disk or out of the config,
and nothing here raises -- a missing model, no output device, no session bus and an unreadable
``config.toml`` are all lines. The exit code carries the one verdict: non-zero when the app
could not work as configured.

The digest check is :class:`~utter.tts.model.ModelStore`'s, not a second hasher: the store is
what refuses a mismatched file at startup, so a diagnosis that disagreed with it would be worse
than none. A file with no recorded digest counts as verified, because there is nothing to check
it against -- the per-file lines say so, which is where a missing ``install`` record shows up.
This is a module a broken install still has to be able to import: it imports ``sounddevice`` and
:mod:`utter.tts.model`, so the CLI loads it inside ``utter doctor`` rather than for every command.
"""

from __future__ import annotations

import os
import re

import sounddevice as sd

from . import paths
from .autostart import autostart_path
from .config import Config
from .hotkey import APP_ID, MANUAL_INSTRUCTIONS, select_backend
from .tts.model import MODEL_NAME, VOICES_NAME, ModelError, ModelStore

LINE_WIDTH = 16
"""Columns the report's labels are padded to, so the values line up under one another."""

LATENCY_SAMPLES = 5
"""How many ``ttfirst`` samples the report shows: enough to see the spread, not a log dump."""

_LATENCY = re.compile(r"ttfirst=([0-9]+\.[0-9]+)")
"""The engine's per-utterance time-to-first-audio field; ``ttfirst=-`` is not a sample."""

_SESSION_UNKNOWN = "unset"
"""What the report calls an unset ``XDG_SESSION_TYPE``, rather than printing nothing."""


def line(label: str, value: str) -> str:
    """One report line: the label padded so the values form a column."""
    return f"{label:<{LINE_WIDTH}} {value}"


def diagnose(config: Config, model: ModelStore | None = None) -> tuple[int, list[str]]:
    """Report the resolved setup as an exit code and the lines to print.

    ``config`` is what the app would run with -- the file plus any ``UTTER_*`` overrides -- and
    ``model`` lets a caller that already holds a :class:`~utter.tts.model.ModelStore` for the
    same models directory hand it over instead of paying for a second one. The code is non-zero only for the ways this setup
    cannot work as configured: a pack file missing or failing its digest, no output device to
    play on, or a ``config.toml`` that could not be read. Nothing here raises, because a
    diagnosis of a broken setup that itself dies is no diagnosis at all.
    """
    broken = 0
    lines: list[str] = []

    config_state, config_readable = _config_report()
    if not config_readable:
        broken += 1
    lines.append(line("config:", config_state))
    lines.extend(_config_lines(config))

    backend, backend_line = _backend_report(config)
    lines.append(line("hotkey backend:", backend_line))
    if backend == "none":
        lines.append(line("hotkey help:", MANUAL_INSTRUCTIONS))

    device, device_usable = _device_report()
    if not device_usable:
        broken += 1
    lines.append(line("output device:", device))

    if model is None:
        models_ok, detail = verify_models(config)
    else:
        models_ok, detail = verify_models(config, model)
    if not models_ok:
        broken += 1
    lines.append(line("models:", str(paths.models_dir())))
    lines.extend(_model_lines(config, models_ok))
    lines.append(line("pack check:", "ok" if models_ok else detail))

    lines.append(line("paths:", f"log={paths.log_path()} socket={paths.socket_path()}"))
    lines.append(line("latency:", _latency_report()))

    return (1 if broken else 0), lines


def verify_models(config: Config, model: ModelStore | None = None) -> tuple[bool, str]:
    """Check the pack against the digests the installer recorded: ``(ok, detail)``.

    ``detail`` is the store's own message when a file is missing or its digest does not match
    the one recorded for it, and ``"ok"`` otherwise. Never raises: the store resolves both the
    file and the digest it is checked against, so a pack that cannot be read is reported rather
    than thrown -- which is what the caller came to find out.
    """
    store = model if model is not None else _store(config)
    try:
        store.verify()
    except (ModelError, OSError, ValueError) as exc:
        return False, str(exc)
    return True, "ok"


def _store(config: Config) -> ModelStore:
    """The pack's store, with the digests the installer recorded in the config."""
    return ModelStore(
        paths.models_dir(),
        {
            MODEL_NAME: config.install.model_sha256,
            VOICES_NAME: config.install.voices_sha256,
        },
    )


def _config_report() -> tuple[str, bool]:
    """The config file's path and whether it could be read, checked rather than assumed.

    ``diagnose`` is handed an already-loaded config, so this is the only place the user can
    learn that the file they edited was skipped: an unreadable or invalid file is reported
    with the parser's own message, and a file that is simply absent is not a failure, because
    the built-in defaults are what the app runs with by design.
    """
    path = paths.config_path()
    if not path.is_file():
        return f"{path} (missing: the built-in defaults are in use)", True
    try:
        Config.load(path)
    except Exception as exc:  # a TOML error, an encoding error, a permission error
        return f"{path} (unreadable: {exc}; the built-in defaults are in use)", False
    return str(path), True


def _config_lines(config: Config) -> list[str]:
    """The resolved values, one line per section: what the app is actually running with."""
    general, speech = config.general, config.speech
    hotkey, ui, install = config.hotkey, config.ui, config.install
    # The autostart key is the intent and ``~/.config/autostart/utter.desktop`` its
    # materialisation, so the line names both: a file left over from an earlier setting (or one
    # a config edited by hand has not yet reconciled) is the one thing the key alone cannot show.
    autostart_file = autostart_path()
    materialised = "file present" if autostart_file.exists() else "file absent"
    lang = f"auto ({speech.effective_lang})" if speech.lang == "auto" else speech.lang
    return [
        line(
            "general:",
            f"log_level={general.log_level} autostart={general.autostart} ({materialised})",
        ),
        line(
            "speech:",
            f"voice={speech.voice} speed={speech.speed} lang={lang} "
            f"phonemizer={speech.phonemizer} normalize={speech.normalize} "
            f"max_chars={speech.max_chars} pause_sentence={speech.pause_sentence} "
            f"pause_clause={speech.pause_clause}",
        ),
        line("hotkey:", f"speak={hotkey.speak} stop={hotkey.stop} pause={hotkey.pause}"),
        line("ui:", f"show_overlay={ui.show_overlay} overlay_pos={ui.overlay_pos}"),
        line("install:", f"consent_given={install.consent_given}"),
    ]


def _backend_report(config: Config) -> tuple[str, str]:
    """The backend the session selects and the portal app-id it registers: name and line.

    The backend comes from the app's own :func:`~utter.hotkey.select_backend`, so the report
    names what the tray would build rather than re-deriving the rule here. The app-id is
    constant and is what the portal registers, so it is shown whatever the backend: with
    ``none`` it is the id the missing desktop entry would have to carry.
    """
    session = os.environ.get("XDG_SESSION_TYPE", "") or _SESSION_UNKNOWN
    portal_available = bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS"))
    backend = select_backend(
        config.hotkey, session_type=session, portal_available=portal_available
    )
    reachable = "portal reachable" if portal_available else "no portal reachable"
    return backend, (
        f"{backend} (configured {config.hotkey.backend}, session {session}, "
        f"{reachable}, portal app-id {APP_ID})"
    )


def _device_report() -> tuple[str, bool]:
    """The default output device's name, or why there is none. Never raises.

    A device record that cannot even be read is treated as no device: either way the app has
    nowhere to play, which is the setup the doctor exists to name.
    """
    try:
        device = sd.query_devices(kind="output")
        return (
            f"{device['name']} ({device['max_output_channels']} channels, "
            f"{float(device['default_samplerate']):.0f} Hz)",
            True,
        )
    except Exception as exc:  # sounddevice's own error type, and anything like it
        return f"unavailable: {exc}", False


def _model_lines(config: Config, models_ok: bool) -> list[str]:
    """Each pack file's path, the digest the installer recorded and the check's result.

    The result is derived from the store's verdict plus this file's own presence, so it can
    never promise more than ``verify_models`` established: a file the check never reached --
    it stops at the first failure -- is reported as unverified rather than assumed good.
    """
    lines = []
    for name, digest in (
        (MODEL_NAME, config.install.model_sha256),
        (VOICES_NAME, config.install.voices_sha256),
    ):
        path = paths.models_dir() / name
        if not path.is_file():
            state = "missing"
        elif not models_ok:
            state = "not verified: the pack check failed"
        elif not digest:
            state = "present, but no digest was recorded for it"
        else:
            state = "sha256 ok"
        lines.append(f"  {path}  sha256={digest or '(not recorded)'}  {state}")
    return lines


def _latency_report() -> str:
    """The last few time-to-first-audio samples the engine logged, ready to print.

    The values are the log's own text, not reformatted, so a sample in the report can be found
    in the log with a plain search. A missing or unreadable log is a line like any other: a
    fresh install has no samples, which is not a fault.
    """
    log = paths.log_path()
    if not log.is_file():
        return f"no samples: {log} does not exist yet"
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"unreadable: {exc}"
    samples = _LATENCY.findall(text)[-LATENCY_SAMPLES:]
    if not samples:
        return f"no samples yet in {log}"
    return (
        f"{' '.join(samples)} (seconds to first audio, oldest first, last {len(samples)} "
        f"utterances; see {log})"
    )
