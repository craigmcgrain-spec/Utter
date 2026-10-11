"""The on-disk model store: digest verification, session options, one warm session.

The installer fetches the model files and records their digests; this module only
checks what it finds on disk. It contains no download code by design.

``ModelStore.session()`` returns the Kokoro wrapper (the object with ``.create()``
and ``.voices``) built around the process's single ONNX session, which stays inside
the store. There is deliberately no teardown: spec D12 loads the model once and the
process exits with it, because recreating a session costs a measured 1282 MiB peak.
"""

import hashlib
from pathlib import Path
from typing import Callable

import onnxruntime as ort
from kokoro_onnx import Kokoro

MODEL_NAME = "kokoro-v1.0.int8.onnx"
VOICES_NAME = "voices-v1.0.bin"

_READ_CHUNK = 1 << 20


class ModelError(RuntimeError):
    """A model file is missing or does not match the digest the installer recorded."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_READ_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


SessionFactory = Callable[[str, ort.SessionOptions, list[str]], object]
KokoroFactory = Callable[[object, str], object]


def _default_session_factory(
    model_path: str, sess_options: ort.SessionOptions, providers: list[str]
) -> ort.InferenceSession:
    """Build the inference session, silencing onnxruntime's process-wide telemetry first."""
    disable_telemetry = getattr(ort, "disable_telemetry_events", None)
    if disable_telemetry is not None:
        disable_telemetry()
    set_severity = getattr(ort, "set_default_logger_severity", None)
    if set_severity is not None:
        set_severity(3)
    return ort.InferenceSession(model_path, sess_options=sess_options, providers=providers)


class ModelStore:
    """Verify the model files, then hand out the process's one warm Kokoro wrapper.

    ``session()`` returns the wrapper, not the raw ONNX ``InferenceSession``; the raw
    session is never exposed and is created once.
    """

    def __init__(
        self,
        models_dir: Path,
        digests: dict[str, str],
        session_factory: SessionFactory | None = None,
        kokoro_factory: KokoroFactory | None = None,
    ) -> None:
        self._models_dir = Path(models_dir)
        self._digests = digests
        self._session_factory = session_factory or _default_session_factory
        self._kokoro_factory = kokoro_factory or Kokoro.from_session
        self._session: object | None = None

    @classmethod
    def build_options(cls) -> ort.SessionOptions:
        """The measured CPU session settings, kept free of process-wide side effects."""
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.enable_cpu_mem_arena = False
        return options

    def verify(self) -> None:
        """Raise ModelError unless both files exist and match any recorded digest."""
        for name in (MODEL_NAME, VOICES_NAME):
            path = self._models_dir / name
            if not path.is_file():
                raise ModelError(f"model file not found: {path}")
            expected = self._digests.get(name, "")
            if not expected:
                continue
            actual = _sha256(path)
            if actual != expected:
                raise ModelError(
                    f"sha256 mismatch for {name}: expected {expected}, got {actual}"
                )

    def session(self) -> object:
        """Return the shared Kokoro wrapper, verifying and building it at most once."""
        if self._session is None:
            self.verify()
            session = self._session_factory(
                str(self._models_dir / MODEL_NAME),
                self.build_options(),
                ["CPUExecutionProvider"],
            )
            self._session = self._kokoro_factory(
                session, str(self._models_dir / VOICES_NAME)
            )
        return self._session

    def voice_names(self) -> list[str]:
        """The sorted voice ids of the loaded pack, for the tray menu and ``utter voices``."""
        return sorted(self.session().voices.files)
