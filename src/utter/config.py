"""User configuration: dataclass schema, TOML load/save and UTTER_* env overrides."""

import dataclasses
import logging
import os
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

from . import paths

SPEED_MIN = 0.75
SPEED_MAX = 2.0
PHONEMIZERS = frozenset({"espeak"})
HOTKEY_BACKENDS = frozenset({"auto", "portal", "x11", "none"})

_LOG_LEVELS = logging.getLevelNamesMapping()
"""The level names the stdlib accepts, so a typo is refused here like any other bad value."""

_SECTION_NAMES = ("general", "speech", "hotkey", "ui", "install")


def _coerce(value, typ):
    """Coerce a TOML value to the field's type."""
    if typ is bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if typ is int:
        return int(value)
    if typ is float:
        return float(value)
    if typ is tuple:
        # TOML arrays; env strings are handled by _coerce_env.
        return tuple(int(part) for part in value)
    return str(value)


def _coerce_env(raw: str, typ, env_key: str, default):
    """Coerce an env override; tuple fields take exactly as many ints as the default."""
    if typ is not tuple:
        return _coerce(raw, typ)
    parts = raw.split(",")
    if len(parts) != len(default):
        raise ValueError(
            f"{env_key} expects {len(default)} comma-separated integers, got {raw!r}"
        )
    try:
        return tuple(int(part) for part in parts)
    except ValueError as exc:
        raise ValueError(
            f"{env_key} is not a comma-separated list of integers: {raw!r}"
        ) from exc


def _from_dict(cls, data: dict):
    """Build a dataclass from a mapping, ignoring keys the schema does not know."""
    types = _FIELD_TYPES[cls]
    section = cls.__name__.lower()
    values = {}
    for name, typ in types.items():
        if name not in data:
            continue
        try:
            value = _coerce(data[name], typ)
        except (TypeError, ValueError) as exc:
            # A field of the wrong shape -- a list where a float belongs -- used to leave this
            # as a bare TypeError from inside ``int``/``float``/``tuple``, which no caller's
            # boundary catches. A config that cannot be read has to be the ValueError they all
            # report, and the message has to name the key that was wrong.
            raise ValueError(
                f"[{section}] {name} is not a valid {typ.__name__}: {data[name]!r}"
            ) from exc
        if typ is tuple:
            # A tuple field is a fixed-length pair of ints (``overlay_pos``). The env path
            # already checks the arity; without the same check here a wrong-length value would
            # crash wherever it is unpacked, naming neither the key nor the file it came from.
            expected = len(getattr(cls, name))
            if len(value) != expected:
                raise ValueError(
                    f"[{section}] {name} expects {expected} integers, got {value!r}"
                )
        values[name] = value
    return cls(**values)


def _section(data: dict, name: str) -> dict:
    """The table a section is built from, refusing a value that is not one.

    A section replaced by a plain value -- ``speech = 5`` instead of a ``[speech]`` table -- used
    to reach the field loop as a ``TypeError`` ("argument of type 'int' is not iterable"): not
    the ``ValueError`` every caller handles, and a message that names neither the key nor the
    file. ``utter doctor``, whose contract is that an unreadable ``config.toml`` is one of its
    lines, died on it.
    """
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"[{name}] must be a table, got {value!r}")
    return value


@dataclass
class General:
    autostart: bool = False
    log_level: str = "info"

    def __post_init__(self) -> None:
        # ``setup_logging`` resolves this name with ``logging.getLevelName``, which answers an
        # unknown one with the string ``"Level verbose"`` and then fails inside ``setLevel`` with
        # a message that names neither the key nor the value. Refusing it here is what the other
        # enum-ish fields do, and it is what turns a typo into ``utter doctor``'s own line.
        if self.log_level.upper() not in _LOG_LEVELS:
            raise ValueError(f"unknown log level: {self.log_level!r}")

    @classmethod
    def from_dict(cls, data: dict) -> "General":
        return _from_dict(cls, data)


VOICE_LANGS = {
    "a": "en-us",
    "b": "en-gb",
    "e": "es",
    "f": "fr-fr",
    "h": "hi",
    "i": "it",
    "j": "ja",
    "p": "pt-br",
    "z": "cmn",
}
"""espeak-ng language per Kokoro voice-id prefix (``bf_emma`` -> ``en-gb``), spec §7.5."""


@dataclass
class Speech:
    voice: str = "af_heart"
    speed: float = 1.0
    lang: str = "auto"  # "auto" follows the voice's prefix; anything else is an espeak-ng code
    phonemizer: str = "espeak"
    normalize: bool = True
    max_chars: int = 20000
    pause_sentence: float = 0.25
    pause_clause: float = 0.12

    def __post_init__(self) -> None:
        self.speed = min(SPEED_MAX, max(SPEED_MIN, float(self.speed)))
        if self.phonemizer not in PHONEMIZERS:
            raise ValueError(f"unknown phonemizer: {self.phonemizer!r}")

    @classmethod
    def from_dict(cls, data: dict) -> "Speech":
        return _from_dict(cls, data)

    @property
    def effective_lang(self) -> str:
        """The language to phonemise in: ``lang`` itself, or the voice's when it is ``auto``.

        A property rather than a stored value, so a voice picked in the tray brings its
        language with it on the next unit; an unknown prefix falls back to ``en-us``.
        """
        if self.lang != "auto":
            return self.lang
        return VOICE_LANGS.get(self.voice[:1], "en-us")


@dataclass
class Hotkey:
    backend: str = "auto"
    speak: str = "LOGO+ALT+s"
    stop: str = "LOGO+ALT+x"
    pause: str = "LOGO+ALT+p"

    def __post_init__(self) -> None:
        if self.backend not in HOTKEY_BACKENDS:
            raise ValueError(f"unknown hotkey backend: {self.backend!r}")

    @classmethod
    def from_dict(cls, data: dict) -> "Hotkey":
        return _from_dict(cls, data)


@dataclass
class Ui:
    show_overlay: bool = True
    overlay_pos: tuple[int, int] = (0, 0)

    @classmethod
    def from_dict(cls, data: dict) -> "Ui":
        return _from_dict(cls, data)


@dataclass
class Install:
    consent_given: bool = False
    model_sha256: str = ""
    voices_sha256: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "Install":
        return _from_dict(cls, data)


_FIELD_TYPES: dict[type, dict[str, type]] = {
    General: {"autostart": bool, "log_level": str},
    Speech: {
        "voice": str,
        "speed": float,
        "lang": str,
        "phonemizer": str,
        "normalize": bool,
        "max_chars": int,
        "pause_sentence": float,
        "pause_clause": float,
    },
    Hotkey: {"backend": str, "speak": str, "stop": str, "pause": str},
    Ui: {"show_overlay": bool, "overlay_pos": tuple},
    Install: {"consent_given": bool, "model_sha256": str, "voices_sha256": str},
}


def _plain(value):
    """Convert tuples (and nested containers) to TOML-serialisable lists."""
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


@dataclass
class Config:
    general: General = field(default_factory=General)
    speech: Speech = field(default_factory=Speech)
    hotkey: Hotkey = field(default_factory=Hotkey)
    ui: Ui = field(default_factory=Ui)
    install: Install = field(default_factory=Install)
    # (section, key) -> (the file's value, the environment's value) for every key
    # merged_with_env overrode. Not part of the schema: save() reads it so that an override
    # stays per-invocation instead of being written into config.toml.
    _env_overrides: dict = field(default_factory=dict, init=False, repr=False, compare=False)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """Load config.toml, falling back to defaults for a missing file or section."""
        path = paths.config_path() if path is None else Path(path)
        if not path.exists():
            return cls()
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        return cls(
            general=General.from_dict(_section(data, "general")),
            speech=Speech.from_dict(_section(data, "speech")),
            hotkey=Hotkey.from_dict(_section(data, "hotkey")),
            ui=Ui.from_dict(_section(data, "ui")),
            install=Install.from_dict(_section(data, "install")),
        )

    def save(self, path: Path | None = None) -> None:
        """Write config.toml atomically via a temporary file plus os.replace.

        A key still holding the value an ``UTTER_<SECTION>_<KEY>`` override gave it is written
        with the value it had before the override; a key changed since (the tray's voice item
        while ``UTTER_SPEECH_VOICE`` is set) is written as changed.
        """
        path = paths.config_path() if path is None else Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {name: dataclasses.asdict(getattr(self, name)) for name in _SECTION_NAMES}
        for (section_name, field_name), (file_value, env_value) in self._env_overrides.items():
            if data[section_name][field_name] == env_value:
                data[section_name][field_name] = file_value
        payload = tomli_w.dumps(_plain(data))
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, path)
        except BaseException:
            os.unlink(tmp_name)
            raise

    def merged_with_env(self) -> "Config":
        """Return a copy with UTTER_<SECTION>_<KEY> environment overrides applied."""
        sections = {}
        env_overrides = {}
        for section_name in _SECTION_NAMES:
            section = getattr(self, section_name)
            updates = {}
            for field_name, typ in _FIELD_TYPES[type(section)].items():
                env_key = f"UTTER_{section_name.upper()}_{field_name.upper()}"
                raw = os.environ.get(env_key)
                if raw is not None:
                    file_value = getattr(section, field_name)
                    updates[field_name] = _coerce_env(raw, typ, env_key, file_value)
                    env_overrides[section_name, field_name] = (file_value, updates[field_name])
            sections[section_name] = dataclasses.replace(section, **updates)
        merged = Config(**sections)
        merged._env_overrides = env_overrides
        return merged
