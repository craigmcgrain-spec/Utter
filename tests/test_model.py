"""The model store: digest verification, session options and one warm session."""

import numpy as np
import onnxruntime as ort
import pytest

from utter.tts.model import MODEL_NAME, VOICES_NAME, ModelError, ModelStore


def make_store(tmp_path, digests, session_factory):
    """A store over an on-disk model and voices file, with the session stubbed out."""
    (tmp_path / MODEL_NAME).write_bytes(b"model")
    (tmp_path / VOICES_NAME).write_bytes(b"voices")
    return ModelStore(tmp_path, digests, session_factory=session_factory)


def test_missing_file_raises(tmp_path):
    with pytest.raises(ModelError, match="kokoro-v1.0.int8.onnx"):
        ModelStore(tmp_path, {}).verify()


def test_digest_mismatch_raises(tmp_path):
    store = make_store(
        tmp_path,
        {MODEL_NAME: "00" * 32, VOICES_NAME: ""},
        lambda path, sess_options, providers: object(),
    )
    with pytest.raises(ModelError, match="sha256"):
        store.verify()


def test_session_is_created_once(tmp_path):
    (tmp_path / MODEL_NAME).write_bytes(b"model")
    (tmp_path / VOICES_NAME).write_bytes(b"voices")
    calls = []

    def factory(path, sess_options, providers):
        calls.append(path)
        return object()

    store = ModelStore(
        tmp_path,
        {},
        session_factory=factory,
        kokoro_factory=lambda session, voices: object(),
    )
    first, second = store.session(), store.session()
    assert first is second
    assert len(calls) == 1


def test_voice_names_reads_the_pack_without_building_the_session(tmp_path):
    calls = []

    def factory(path, sess_options, providers):
        calls.append(path)
        return object()

    with (tmp_path / VOICES_NAME).open("wb") as pack:
        np.savez(pack, af_heart=np.zeros(1), bf_emma=np.zeros(1))
    store = ModelStore(
        tmp_path,
        {},
        session_factory=factory,
        kokoro_factory=lambda session, voices: object(),
    )
    assert store.voice_names() == ["af_heart", "bf_emma"]
    assert calls == [], "listing voices must not load the model"


def test_session_options_match_spec():
    options = ModelStore.build_options()
    assert options.intra_op_num_threads == 4
    assert options.execution_mode == ort.ExecutionMode.ORT_SEQUENTIAL
    assert options.enable_cpu_mem_arena is False


def test_default_session_factory_passes_cpu_provider_only(tmp_path):
    captured = {}

    def fake_ort_session(path, sess_options, providers):
        captured["providers"] = providers
        return object()

    (tmp_path / MODEL_NAME).write_bytes(b"m")
    (tmp_path / VOICES_NAME).write_bytes(b"v")
    store = ModelStore(
        tmp_path,
        {},
        session_factory=fake_ort_session,
        kokoro_factory=lambda session, voices: object(),
    )
    store.session()
    assert captured["providers"] == ["CPUExecutionProvider"]
