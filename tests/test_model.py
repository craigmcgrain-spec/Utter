"""The model store: digest verification, session options and one warm session."""

import hashlib

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

    # np.savez stores members in the order given, so the pack lists bf_emma first and only
    # voice_names()'s sort produces af_heart first. And np.savez on a path appends ".npz",
    # so the pack must be written through a binary handle to land at VOICES_NAME exactly.
    with (tmp_path / VOICES_NAME).open("wb") as handle:
        np.savez(handle, bf_emma=np.zeros(1), af_heart=np.zeros(1))
    store = ModelStore(
        tmp_path,
        {},
        session_factory=factory,
        kokoro_factory=lambda session, voices: object(),
    )
    assert store.voice_names() == ["af_heart", "bf_emma"]
    assert calls == [], "listing voices must not load the model"


def test_matching_digests_verify_and_build(tmp_path):
    (tmp_path / MODEL_NAME).write_bytes(b"model")
    (tmp_path / VOICES_NAME).write_bytes(b"voices")
    digests = {
        MODEL_NAME: hashlib.sha256(b"model").hexdigest(),
        VOICES_NAME: hashlib.sha256(b"voices").hexdigest(),
    }
    built = []
    wrapped = []

    def factory(path, sess_options, providers):
        session = object()
        built.append((path, session))
        return session

    def kokoro_factory(session, voices):
        wrapped.append((session, voices))
        return object()

    store = ModelStore(
        tmp_path, digests, session_factory=factory, kokoro_factory=kokoro_factory
    )
    store.verify()
    first, second = store.session(), store.session()
    assert first is second
    assert [path for path, _ in built] == [str(tmp_path / MODEL_NAME)]
    assert wrapped == [(built[0][1], str(tmp_path / VOICES_NAME))]


def test_session_verifies_before_building(tmp_path):
    (tmp_path / MODEL_NAME).write_bytes(b"model")
    (tmp_path / VOICES_NAME).write_bytes(b"voices")
    calls = []

    def factory(path, sess_options, providers):
        calls.append(path)
        return object()

    store = ModelStore(
        tmp_path,
        {MODEL_NAME: "00" * 32},
        session_factory=factory,
        kokoro_factory=lambda session, voices: object(),
    )
    with pytest.raises(ModelError, match="sha256"):
        store.session()
    assert calls == [], "the session must not be built before verification passes"


def test_each_model_file_is_hashed_once_per_process(tmp_path, monkeypatch):
    # The CLI verifies before it opens a device, session() verifies again before building it,
    # and voice_names() checks the pack before reading it -- so without a remembered verdict a
    # cold start reads and hashes both files two or three times over, which is exactly what a
    # cold-start measurement must not be paying for.
    (tmp_path / MODEL_NAME).write_bytes(b"model")
    # np.savez on a path appends ".npz", so the pack is written through a binary handle.
    with (tmp_path / VOICES_NAME).open("wb") as handle:
        np.savez(handle, af_heart=np.zeros(1))
    digests = {
        MODEL_NAME: hashlib.sha256((tmp_path / MODEL_NAME).read_bytes()).hexdigest(),
        VOICES_NAME: hashlib.sha256((tmp_path / VOICES_NAME).read_bytes()).hexdigest(),
    }
    passes = []
    real = hashlib.sha256
    monkeypatch.setattr("utter.tts.model.hashlib.sha256", lambda: passes.append(1) or real())
    store = ModelStore(
        tmp_path,
        digests,
        session_factory=lambda path, sess_options, providers: object(),
        kokoro_factory=lambda session, voices: object(),
    )
    store.verify()
    store.session()
    assert store.voice_names() == ["af_heart"]
    assert len(passes) == 2, "one hash for each of the two files, not one per verify call"


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


def test_a_failing_build_is_reported_as_a_model_error(tmp_path):
    """A file that verifies but cannot be loaded must not escape as a foreign exception.

    With no digest recorded a corrupt or truncated file passes ``verify()`` and reaches the
    session build, where ONNX Runtime raises types that derive from bare ``Exception``. The
    callers only know ``ModelError``, so this pins the translation (and the retry: nothing
    failed is cached).
    """
    (tmp_path / MODEL_NAME).write_bytes(b"model")
    (tmp_path / VOICES_NAME).write_bytes(b"voices")

    class OrtLike(Exception):
        """Stands in for onnxruntime's own error types, which are not RuntimeErrors."""

    calls = []

    def factory(path, sess_options, providers):
        calls.append(path)
        raise OrtLike("[ONNXRuntimeError] : 7 : INVALID_PROTOBUF : not a graph")

    store = ModelStore(tmp_path, {}, session_factory=factory)
    for _ in range(2):
        with pytest.raises(ModelError, match="cannot load the model"):
            store.session()
    assert len(calls) == 2, "a failed build must not be cached as the session"


def test_an_unreadable_voice_pack_is_reported_as_a_model_error(tmp_path):
    """A pack that is not a voices archive is a model error, not a numpy one."""
    (tmp_path / MODEL_NAME).write_bytes(b"model")
    (tmp_path / VOICES_NAME).write_bytes(b"not an npz at all")
    store = ModelStore(tmp_path, {}, session_factory=lambda *args: object())
    with pytest.raises(ModelError, match="voice pack"):
        store.voice_names()
