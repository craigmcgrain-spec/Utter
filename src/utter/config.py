"""User configuration: dataclass schema, TOML load/save and UTTER_* env overrides."""

import dataclasses
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

_SECTION_NAMES = ("general", "speech", "hotkey", "ui", "install")


def _coerce(value, typ):
    """Coerce a TOML value or env string to the field's type."""
    if typ is bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if typ is int:
        return int(value)
    if typ is float:
        return float(value)
    if typ is tuple:
        return tuple(int(part) for part in value)
    return str(value)


def _from_dict(cls, data: dict):
    """Build a dataclass from a mapping, ignoring keys the schema does not know."""
    types = _FIELD_TYPES[cls]
    return cls(
        **{
            name: _coerce(data[name], typ)
            for name, typ in types.items()
            if name in data
        }
    )


@dataclass
class General:
    autostart: bool = False
    log_level: str = "info"

    @classmethod
    def from_dict(cls, data: dict) -> "General":
        return _from_dict(cls, data)


@dataclass
class Speech:
    voice: str = "af_heart"
    speed: float = 1.0
    lang: str = "en-us"
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

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """Load config.toml, falling back to defaults for a missing file or section."""
        path = paths.config_path() if path is None else Path(path)
        if not path.exists():
            return cls()
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        return cls(
            general=General.from_dict(data.get("general", {})),
            speech=Speech.from_dict(data.get("speech", {})),
            hotkey=Hotkey.from_dict(data.get("hotkey", {})),
            ui=Ui.from_dict(data.get("ui", {})),
            install=Install.from_dict(data.get("install", {})),
        )

    def save(self, path: Path | None = None) -> None:
        """Write config.toml atomically via a temporary file plus os.replace."""
        path = paths.config_path() if path is None else Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = tomli_w.dumps(_plain(dataclasses.asdict(self)))
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
        overrides = {}
        for section_name in _SECTION_NAMES:
            section = getattr(self, section_name)
            updates = {}
            for field_name, typ in _FIELD_TYPES[type(section)].items():
                raw = os.environ.get(f"UTTER_{section_name.upper()}_{field_name.upper()}")
                if raw is not None:
                    updates[field_name] = _coerce(raw, typ)
            overrides[section_name] = dataclasses.replace(section, **updates)
        return Config(**overrides)
