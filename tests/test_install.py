"""``install.sh`` end to end: a real install into a temporary prefix, and its failure paths.

Every test runs the script as a subprocess, because the shell script *is* the artifact under
test -- a Python reimplementation would not exercise curl, ``sha256sum`` or the atomic move.
Nothing here reaches the network or the real ``$HOME``: ``--prefix`` maps every root the
installer writes to inside the temporary directory, and ``fake_release`` serves the release
files from 127.0.0.1 on an ephemeral port so the digest path runs without paying for 115 MB.
"""

import hashlib
import http.server
import os
import shutil
import socketserver
import subprocess
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

PAYLOAD = {
    "good": b"pretend release payload\n",
    "wrong": b"tampered release payload\n",
}
"""One stand-in payload per mode, shared by both file names: the installer treats the two
downloads identically, so a second payload and a second server would only add seconds."""

GOOD_SHA256 = hashlib.sha256(PAYLOAD["good"]).hexdigest()
"""The digest the fixture's URL overrides are paired with. ``"wrong"`` mode serves bytes that
do not hash to it, which is the tampered-mirror case the digest check exists for."""


def run(args, env=None, input=None):
    """Run ``args`` from the repository root with ``env`` layered over this process's.

    ``input`` feeds stdin. Without it stdin is ``/dev/null``, so a test that meets an
    unexpected prompt gets a deterministic EOF instead of consuming the suite's stdin.
    """
    extra = {} if input is not None else {"stdin": subprocess.DEVNULL}
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        cwd=REPO,
        input=input,
        env={**os.environ, **(env or {})},
        **extra,
    )


@pytest.fixture
def tmp_prefix(tmp_path):
    """The install root. ``--prefix`` maps every XDG root the installer writes inside it."""
    return tmp_path / "prefix"


@pytest.fixture
def fake_release(monkeypatch):
    """Serve a stand-in release payload from a loopback HTTP server.

    ``fake_release(mode)`` returns the URL for one file and exports both digest overrides in
    the environment (a digest override is only honoured next to its URL override, so the pair
    has to travel together). ``"good"`` serves the bytes the exported digest describes;
    ``"wrong"`` serves different bytes under the same digest, which the installer must reject.
    """
    servers = []

    def serve(contents):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(contents)))
                self.end_headers()
                self.wfile.write(contents)

            def log_message(self, *args):
                pass  # keep the pytest output readable

        httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}/release"

    def release(mode="good"):
        monkeypatch.setenv("UTTER_MODEL_SHA256", GOOD_SHA256)
        monkeypatch.setenv("UTTER_VOICES_SHA256", GOOD_SHA256)
        return serve(PAYLOAD[mode])

    yield release
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def test_dry_run_makes_no_changes(tmp_prefix):
    result = run(["bash", "install.sh", "--dry-run", "--prefix", str(tmp_prefix), "-y"])
    assert result.returncode == 0
    assert "kokoro-v1.0.int8.onnx" in result.stdout
    assert "6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb" in result.stdout
    assert not (tmp_prefix / "share" / "utter" / "venv").exists()

def test_consent_is_required_without_yes(tmp_prefix):
    result = run(["bash", "install.sh", "--dry-run", "--prefix", str(tmp_prefix)], input="n\n")
    assert "download" in result.stdout.lower()
    assert result.returncode != 0

def test_bad_digest_aborts_and_removes_the_file(tmp_prefix, fake_release):
    result = run(["bash", "install.sh", "--prefix", str(tmp_prefix), "-y"],
                 env={"UTTER_MODEL_URL": fake_release("wrong"),
                      "UTTER_VOICES_URL": fake_release("good")})
    assert result.returncode != 0
    assert "sha256" in result.stderr.lower()
    assert not list((tmp_prefix / "share" / "utter" / "models").glob("*.onnx"))

def test_desktop_file_uses_the_absolute_exec_path(tmp_prefix, fake_release):
    run(["bash", "install.sh", "--prefix", str(tmp_prefix), "-y", "--no-autostart"],
        env={"UTTER_MODEL_URL": fake_release("good"), "UTTER_VOICES_URL": fake_release("good")})
    entry = (tmp_prefix / "share" / "applications" / "utter.desktop").read_text()
    assert f"Exec={tmp_prefix}/share/utter/venv/bin/utter" in entry
    assert "Categories=Utility;Accessibility;" in entry

def test_uninstall_leaves_nothing_behind(tmp_prefix, fake_release):
    good = fake_release("good")
    run(["bash", "install.sh", "--prefix", str(tmp_prefix), "-y"],
        env={"UTTER_MODEL_URL": good, "UTTER_VOICES_URL": good})
    run(["bash", "install.sh", "--prefix", str(tmp_prefix), "--uninstall"])
    # config.toml is the one documented keep (spec §10 step 9), and the install creates it to
    # record the digests, so it is the single file a plain uninstall is meant to leave.
    leftovers = [p for p in tmp_prefix.rglob("*")
                 if p.is_file() and "utter" in str(p) and p.name != "config.toml"]
    assert leftovers == []
    assert (tmp_prefix / "config" / "utter" / "config.toml").is_file()


def test_the_consent_default_is_no(tmp_prefix):
    """Accepting the prompt means typing ``y``: an empty answer must decline."""
    result = run(["bash", "install.sh", "--dry-run", "--prefix", str(tmp_prefix)], input="\n")
    assert result.returncode != 0
    assert "declin" in result.stdout.lower()
    assert not tmp_prefix.exists()


def test_a_digest_override_alone_cannot_weaken_a_pinned_download(tmp_prefix):
    """The digest override is honoured only next to its URL override, so a stray
    ``UTTER_MODEL_SHA256`` cannot re-point a download that still targets the pinned release."""
    result = run(
        ["bash", "install.sh", "--dry-run", "--prefix", str(tmp_prefix), "-y"],
        env={"UTTER_MODEL_SHA256": "0" * 64, "UTTER_VOICES_SHA256": "0" * 64},
    )
    assert result.returncode == 0
    assert "6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb" in result.stdout
    assert "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d" in result.stdout
    assert "0" * 64 not in result.stdout


def test_an_already_correct_file_is_not_downloaded_again(tmp_prefix):
    """A correct file in ``models/`` short-circuits the fetch. Both URLs are unreachable, so a
    successful install proves nothing was downloaded -- and the files are left untouched."""
    models = tmp_prefix / "share" / "utter" / "models"
    models.mkdir(parents=True)
    for name in ("kokoro-v1.0.int8.onnx", "voices-v1.0.bin"):
        (models / name).write_bytes(PAYLOAD["good"])
    result = run(
        ["bash", "install.sh", "--prefix", str(tmp_prefix), "-y", "--no-autostart"],
        env={
            "UTTER_MODEL_URL": "http://127.0.0.1:1/kokoro-v1.0.int8.onnx",
            "UTTER_MODEL_SHA256": GOOD_SHA256,
            "UTTER_VOICES_URL": "http://127.0.0.1:1/voices-v1.0.bin",
            "UTTER_VOICES_SHA256": GOOD_SHA256,
        },
    )
    assert result.returncode == 0, result.stderr
    assert "already present" in result.stdout
    assert "kokoro-v1.0.int8.onnx" in result.stdout
    assert (models / "kokoro-v1.0.int8.onnx").read_bytes() == PAYLOAD["good"]
    assert not list((tmp_prefix / "share" / "utter").rglob("*.part"))


def test_shim_and_autostart_entry_reach_the_venv(tmp_prefix, fake_release):
    """The shim repairs what a desktop launch does not give a program -- a PATH -- and the
    autostart entry points at the same absolute binary as the launcher."""
    good = fake_release("good")
    result = run(
        ["bash", "install.sh", "--prefix", str(tmp_prefix), "-y"],
        env={"UTTER_MODEL_URL": good, "UTTER_VOICES_URL": good},
    )
    assert result.returncode == 0, result.stderr
    venv_binary = f"{tmp_prefix}/share/utter/venv/bin/utter"
    shim = tmp_prefix / "bin" / "utter"
    assert shim.is_file(), "the shim is what ~/.local/bin/utter is"
    assert os.access(shim, os.X_OK)
    body = shim.read_text()
    assert venv_binary in body
    assert "PATH=" in body
    entry = (tmp_prefix / "config" / "autostart" / "utter.desktop").read_text()
    assert f"Exec={venv_binary}" in entry


def test_dry_run_creates_nothing_at_all(tmp_prefix, fake_release):
    """Not even an empty prefix directory: a dry run reports the whole plan and writes none
    of it, naming every artifact it would write."""
    good = fake_release("good")
    result = run(
        ["bash", "install.sh", "--dry-run", "--prefix", str(tmp_prefix), "-y"],
        env={"UTTER_MODEL_URL": good, "UTTER_VOICES_URL": good},
    )
    assert result.returncode == 0, result.stderr
    assert not tmp_prefix.exists()
    for artifact in ("kokoro-v1.0.int8.onnx", "voices-v1.0.bin", "utter.desktop",
                     "utter.svg", "bin/utter"):
        assert artifact in result.stdout, artifact
    assert "would write" in result.stdout


def test_the_installer_records_the_digests_in_the_apps_config(tmp_prefix, fake_release):
    """Spec §10 step 4: a fresh install records both pinned digests so the runtime and
    ``utter doctor`` verify the model files. The shape is the app's own, and values a config
    already holds survive the recording."""
    good = fake_release("good")
    env = {"UTTER_MODEL_URL": good, "UTTER_VOICES_URL": good}
    install = ["bash", "install.sh", "--prefix", str(tmp_prefix), "-y", "--no-autostart"]
    run(install, env=env)
    config = tmp_prefix / "config" / "utter" / "config.toml"
    text = config.read_text()
    assert "model_sha256" in text and "voices_sha256" in text, text
    assert GOOD_SHA256 in text
    assert "consent_given = true" in text

    config.write_text('[speech]\nvoice = "af_heart"\n')  # a config the app already owns
    run(install, env=env)
    text = config.read_text()
    assert "af_heart" in text, "the values the file held must survive the recording"
    assert GOOD_SHA256 in text

    run(["bash", "install.sh", "--prefix", str(tmp_prefix), "--uninstall"])
    assert config.exists(), "a plain uninstall keeps config.toml"
    run(["bash", "install.sh", "--prefix", str(tmp_prefix), "--uninstall", "--purge"])
    assert not config.exists()


def test_the_preflight_never_runs_sudo(tmp_path, tmp_prefix):
    """The report prints the command a human should run and never runs it itself. ``uv``
    ships in ``~/.local/bin`` here, so this PATH hides it and the plan must say so."""
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    marker = tmp_path / "sudo-ran"
    sudo = fake_bin / "sudo"
    sudo.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
    sudo.chmod(0o755)
    reduced = f"{fake_bin}:/usr/bin:/bin"
    result = run(
        ["bash", "install.sh", "--dry-run", "--prefix", str(tmp_prefix), "-y"],
        env={"PATH": reduced},
    )
    assert shutil.which("uv", path=reduced) is None, "this PATH must hide uv to test the report"
    assert "Preflight" in result.stdout
    assert "sudo dnf install" in result.stdout
    assert result.returncode != 0, "an install whose prerequisites are missing must not pass"
    assert not marker.exists()
    assert not tmp_prefix.exists()
