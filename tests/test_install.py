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


def unreachable_env():
    """Overrides whose URLs cannot be fetched, paired with the stand-in payload's digest.

    A URL override and its digest override travel together; given both, the installer's own
    "already correct" check satisfies each file from disk, so an install under this env makes no
    request at all.
    """
    return {
        "UTTER_MODEL_URL": "http://127.0.0.1:1/kokoro-v1.0.int8.onnx",
        "UTTER_MODEL_SHA256": GOOD_SHA256,
        "UTTER_VOICES_URL": "http://127.0.0.1:1/voices-v1.0.bin",
        "UTTER_VOICES_SHA256": GOOD_SHA256,
    }


def place_models(prefix):
    """Write the stand-in release files where an install will find them already correct."""
    models = prefix / "share" / "utter" / "models"
    models.mkdir(parents=True, exist_ok=True)
    for name in ("kokoro-v1.0.int8.onnx", "voices-v1.0.bin"):
        (models / name).write_bytes(PAYLOAD["good"])


@pytest.fixture(scope="session")
def warm_prefix(tmp_path_factory):
    """One real install for the session, whose venv the launcher, cache and uninstall tests
    hard-link into their own prefixes.

    A venv holds PyQt6 and onnxruntime, ~400 MB, and building one per test into the temporary
    filesystem this suite shares with a second checkout's test run is what fills it. So it is
    built once, by install.sh itself -- the venv path is covered here and by the verbatim tests
    that install for real -- and hard-linked (one inode set, not a copy) by the tests that are
    about something else. Each of those still runs install.sh, which re-runs `uv venv` and
    `uv pip install` against the linked venv, so a broken venv step still fails them.
    """
    prefix = tmp_path_factory.mktemp("warm") / "prefix"
    place_models(prefix)
    install = ["bash", "install.sh", "--prefix", str(prefix), "-y", "--no-autostart"]
    result = run(install, env=unreachable_env())
    if result.returncode != 0:
        # One retry, because this checkout is shared with a second test run whose uv commands use
        # the same cache: a package install can fail transiently (uv's own message is printed
        # here, so a real regression is visible rather than retried away). Both attempts failing
        # fails the fixture.
        print(f"the session install failed, retrying once:\n{result.stderr}")
        result = run(install, env=unreachable_env())
    assert result.returncode == 0, result.stderr
    return prefix


def seed_venv(prefix, warm_prefix):
    """Hard-link the session's installed venv into ``prefix`` before install.sh runs."""
    target = prefix / "share" / "utter"
    target.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["cp", "-al", str(warm_prefix / "share" / "utter" / "venv"), str(target / "venv")],
        check=True,
        capture_output=True,
    )


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

def test_the_install_creates_a_valid_desktop_entry_with_its_icons(tmp_prefix, fake_release):
    """The launcher is what the app menu lists and what the portal reads the app's name and
    icon from (its basename is the app-id), so it must exist, validate, and name an icon the
    install actually put in the theme -- the full drawing and the small-size one."""
    result = run(["bash", "install.sh", "--prefix", str(tmp_prefix), "-y", "--no-autostart"],
                 env={"UTTER_MODEL_URL": fake_release("good"),
                      "UTTER_VOICES_URL": fake_release("good")})
    assert result.returncode == 0, result.stderr
    entry_path = tmp_prefix / "share" / "applications" / "utter.desktop"
    entry = entry_path.read_text()
    assert f"Exec={tmp_prefix}/share/utter/venv/bin/utter" in entry
    assert "Categories=Utility;Accessibility;" in entry
    assert "Icon=utter" in entry
    if shutil.which("desktop-file-validate"):
        validated = subprocess.run(["desktop-file-validate", str(entry_path)],
                                   capture_output=True, text=True)
        assert validated.returncode == 0, validated.stdout + validated.stderr
    icons = tmp_prefix / "share" / "icons" / "hicolor"
    assert (icons / "scalable" / "apps" / "utter.svg").read_bytes() == (
        Path("src/utter/ui/utter.svg").read_bytes())
    for size in (16, 22, 24):
        assert (icons / f"{size}x{size}" / "apps" / "utter.svg").read_bytes() == (
            Path("src/utter/ui/utter-small.svg").read_bytes())

def test_uninstall_leaves_nothing_behind(tmp_prefix, fake_release):
    good = fake_release("good")
    run(["bash", "install.sh", "--prefix", str(tmp_prefix), "-y"],
        env={"UTTER_MODEL_URL": good, "UTTER_VOICES_URL": good})
    run(["bash", "install.sh", "--prefix", str(tmp_prefix), "--uninstall"])
    # config.toml is the one documented keep (spec §10 step 9), and the install creates it to
    # record the digests, so it is the single path a plain uninstall is meant to leave -- with
    # its own directory. Everything else goes, directories included: an emptied
    # share/utter left behind is residue, and a file-only scan would not see it.
    kept = tmp_prefix / "config" / "utter" / "config.toml"
    leftovers = [p for p in tmp_prefix.rglob("*")
                 if "utter" in str(p) and p != kept and p != kept.parent]
    assert leftovers == []
    assert kept.is_file()


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


def test_an_already_correct_file_is_not_downloaded_again(tmp_prefix, warm_prefix):
    """A correct file in ``models/`` short-circuits the fetch. Both URLs are unreachable, so a
    successful install proves nothing was downloaded -- and the files are left untouched."""
    place_models(tmp_prefix)
    seed_venv(tmp_prefix, warm_prefix)
    models = tmp_prefix / "share" / "utter" / "models"
    result = run(
        ["bash", "install.sh", "--prefix", str(tmp_prefix), "-y", "--no-autostart"],
        env=unreachable_env(),
    )
    assert result.returncode == 0, result.stderr
    assert "already present" in result.stdout
    assert "kokoro-v1.0.int8.onnx" in result.stdout
    assert (models / "kokoro-v1.0.int8.onnx").read_bytes() == PAYLOAD["good"]
    assert not list((tmp_prefix / "share" / "utter").rglob("*.part"))


def test_shim_and_autostart_entry_reach_the_venv(tmp_prefix, warm_prefix, fake_release):
    """The shim repairs what a desktop launch does not give a program -- a PATH -- and the
    autostart entry points at the same absolute binary as the launcher."""
    good = fake_release("good")
    seed_venv(tmp_prefix, warm_prefix)
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


def test_the_installer_records_the_digests_in_the_apps_config(
    tmp_prefix, warm_prefix, fake_release
):
    """Spec §10 step 4: a fresh install records both pinned digests so the runtime and
    ``utter doctor`` verify the model files. The shape is the app's own, and values a config
    already holds survive the recording."""
    good = fake_release("good")
    env = {"UTTER_MODEL_URL": good, "UTTER_VOICES_URL": good}
    seed_venv(tmp_prefix, warm_prefix)
    install = ["bash", "install.sh", "--prefix", str(tmp_prefix), "-y", "--no-autostart"]
    installed = run(install, env=env)
    assert installed.returncode == 0, installed.stderr
    config = tmp_prefix / "config" / "utter" / "config.toml"
    text = config.read_text()
    assert "model_sha256" in text and "voices_sha256" in text, text
    assert GOOD_SHA256 in text
    assert "consent_given = true" in text

    config.write_text('[speech]\nvoice = "af_heart"\n')  # a config the app already owns
    again = run(install, env=env)
    assert again.returncode == 0, again.stderr
    text = config.read_text()
    assert "af_heart" in text, "the values the file held must survive the recording"
    assert GOOD_SHA256 in text

    run(["bash", "install.sh", "--prefix", str(tmp_prefix), "--uninstall"])
    assert config.exists(), "a plain uninstall keeps config.toml"
    run(["bash", "install.sh", "--prefix", str(tmp_prefix), "--uninstall", "--purge"])
    assert not config.exists()


def test_the_installer_records_the_autostart_key(tmp_prefix, warm_prefix, fake_release):
    """The autostart file is the materialisation of ``[general].autostart``, so the installer
    records the key it just answered: without it the file and the key would disagree, and the
    app's tray item -- which reads the key -- would show the opposite of what is on disk."""
    good = fake_release("good")
    env = {"UTTER_MODEL_URL": good, "UTTER_VOICES_URL": good}
    config = tmp_prefix / "config" / "utter" / "config.toml"

    seed_venv(tmp_prefix, warm_prefix)
    installed = run(["bash", "install.sh", "--prefix", str(tmp_prefix), "-y"], env=env)
    assert installed.returncode == 0, installed.stderr
    assert "autostart = true" in config.read_text()
    assert (tmp_prefix / "config" / "autostart" / "utter.desktop").is_file()

    seed_venv(tmp_prefix, warm_prefix)
    opted_out = run(
        ["bash", "install.sh", "--prefix", str(tmp_prefix), "-y", "--no-autostart"], env=env
    )
    assert opted_out.returncode == 0, opted_out.stderr
    assert "autostart = false" in config.read_text(), "the opt-out must be recorded too"


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


def test_an_empty_prefix_is_an_error_not_the_real_home(tmp_path):
    """A wrapper script whose variable is unset must not silently install into -- or with
    ``--purge`` delete -- the user's real roots: an empty ``--prefix`` dies before anything is
    computed from it, which is why nothing, not even the preflight, is printed.

    The destructive form is checked under ``--dry-run`` on purpose: if the guard regressed,
    this test must not be what deletes the developer's own install.
    """
    for args in (["--prefix", ""], ["--prefix="], ["--uninstall", "--purge", "--prefix", ""]):
        result = run(["bash", "install.sh", "--dry-run", "-y", *args])
        assert result.returncode == 2, (args, result.stderr)
        assert "prefix" in result.stderr.lower()
        assert result.stdout == "", "no root may be derived from an empty prefix"


def test_a_prefix_that_normalizes_to_the_root_is_refused():
    """``/``, ``//`` and ``///`` all name the filesystem root: refusing them keeps
    ``--prefix //`` from reaching ``//bin/utter`` through the uninstall's ``rm -rf``."""
    for value in ("/", "//", "///"):
        result = run(["bash", "install.sh", "--dry-run", "-y", "--prefix", value])
        assert result.returncode == 2, (value, result.stderr)
        assert "prefix" in result.stderr.lower()
        assert result.stdout == ""


def test_a_prefix_that_resolves_to_the_root_is_refused(tmp_path):
    """Lexical stripping is not enough: `/.`, `/..`, `/tmp/..` and a symlink to `/` carry no
    trailing slash and every one of them resolves to the filesystem root, where
    `rm -rf -- /../bin/utter` reaches the distribution's own binary through /bin -> usr/bin.

    The value is checked before any root is derived, which is why nothing is printed and why
    the escaped roots (``/share/utter`` and friends) are never created.
    """
    root_link = tmp_path / "root-link"
    root_link.symlink_to("/")
    for value in ("/.", "/..", "/tmp/..", str(root_link)):
        result = run(["bash", "install.sh", "--dry-run", "-y", "--prefix", value])
        assert result.returncode == 2, (value, result.stderr)
        assert "prefix" in result.stderr.lower()
        assert result.stdout == "", "no root may be derived from a prefix that resolves to /"
        assert not Path("/share/utter").exists()
        assert not Path("/bin/utter").exists()


def test_uninstall_removes_a_shim_that_points_into_the_removed_venv(tmp_prefix, warm_prefix,
                                                                   fake_release):
    """A shim left as a link into the venv (an earlier install's arrangement) dangles by the
    time the uninstall reaches it -- and ``[ -e ]`` follows links and cannot see it."""
    good = fake_release("good")
    seed_venv(tmp_prefix, warm_prefix)
    installed = run(["bash", "install.sh", "--prefix", str(tmp_prefix), "-y", "--no-autostart"],
                    env={"UTTER_MODEL_URL": good, "UTTER_VOICES_URL": good})
    assert installed.returncode == 0, installed.stderr
    shim = tmp_prefix / "bin" / "utter"
    venv = tmp_prefix / "share" / "utter" / "venv"
    shim.unlink()
    shim.symlink_to(venv / "bin" / "utter")
    shutil.rmtree(venv)
    assert shim.is_symlink() and not shim.exists(), "the shim must dangle for this to bite"

    result = run(["bash", "install.sh", "--prefix", str(tmp_prefix), "--uninstall"])
    assert result.returncode == 0, result.stderr
    assert not shim.is_symlink(), "a link into the removed venv is residue on the user's PATH"


def test_uninstall_removes_the_directories_it_emptied(tmp_prefix, warm_prefix, fake_release):
    """Spec §11's phase-3 criterion is no residue, so an emptied ``share/utter`` and ``bin/``
    go as well: ``find ~/.local -name "*utter*"`` would otherwise still list them."""
    good = fake_release("good")
    seed_venv(tmp_prefix, warm_prefix)
    installed = run(["bash", "install.sh", "--prefix", str(tmp_prefix), "-y", "--no-autostart"],
                    env={"UTTER_MODEL_URL": good, "UTTER_VOICES_URL": good})
    data = tmp_prefix / "share" / "utter"
    assert installed.returncode == 0, installed.stderr
    assert data.is_dir()
    run(["bash", "install.sh", "--prefix", str(tmp_prefix), "--uninstall"])
    assert not data.exists()
    assert not (tmp_prefix / "bin").exists()


def test_the_venv_provides_the_console_script_every_launcher_names(tmp_prefix, fake_release):
    """The launcher entry, the autostart entry and the shim all name ``$venv_dir/bin/utter``,
    so the installed package must really provide that executable -- otherwise a rename in
    pyproject.toml's ``[project.scripts]`` would leave every launcher exec'ing nothing while
    the text-level assertions still passed."""
    good = fake_release("good")
    installed = run(["bash", "install.sh", "--prefix", str(tmp_prefix), "-y", "--no-autostart"],
                    env={"UTTER_MODEL_URL": good, "UTTER_VOICES_URL": good})
    assert installed.returncode == 0, installed.stderr
    binary = tmp_prefix / "share" / "utter" / "venv" / "bin" / "utter"
    assert binary.is_file(), "the venv must provide the binary the entries exec"
    assert os.access(binary, os.X_OK)
    started = run([str(binary), "--help"])
    assert started.returncode == 0, started.stderr
    assert "usage" in started.stdout.lower()


def test_a_prefix_with_a_space_still_produces_a_usable_entry(tmp_path, warm_prefix,
                                                             fake_release):
    """The Exec key is parsed as an argv, so a launcher path carrying a space has to be quoted:
    measured with GLib, the unquoted form makes the desktop either drop the entry or run the
    program named before the space. TryExec is a path rather than a command line and is looked
    up as written, so it stays unquoted -- quoting it is what makes GLib ignore the entry. The
    shim needs no quoting at all: it already execs the binary inside double quotes, and the
    directory it puts on PATH is its own, not the real home's."""
    prefix = tmp_path / "a prefix with spaces" / "prefix"
    good = fake_release("good")
    seed_venv(prefix, warm_prefix)
    place_models(prefix)
    result = run(
        ["bash", "install.sh", "--prefix", str(prefix), "-y"],
        env={"UTTER_MODEL_URL": good, "UTTER_VOICES_URL": good},
    )
    assert result.returncode == 0, result.stderr
    venv_binary = f"{prefix}/share/utter/venv/bin/utter"
    entry = (prefix / "share" / "applications" / "utter.desktop").read_text()
    assert f'Exec="{venv_binary}"' in entry
    assert f"TryExec={venv_binary}" in entry
    autostart = (prefix / "config" / "autostart" / "utter.desktop").read_text()
    assert f'Exec="{venv_binary}"' in autostart
    shim = (prefix / "bin" / "utter").read_text()
    assert f'exec "{venv_binary}"' in shim
    assert f'PATH="{prefix}/bin:' in shim, "a prefixed shim must not put the real home on PATH"


def test_an_autostart_opt_out_removes_an_existing_entry(tmp_prefix, warm_prefix, fake_release):
    """``[general].autostart`` is the single source of truth and the desktop file its
    materialisation, so an answer of no has to take away the entry an earlier install wrote:
    recording ``autostart = false`` while the file still stands is the disagreement the app's
    own startup reconciliation exists to undo."""
    good = fake_release("good")
    env = {"UTTER_MODEL_URL": good, "UTTER_VOICES_URL": good}
    entry = tmp_prefix / "config" / "autostart" / "utter.desktop"
    config = tmp_prefix / "config" / "utter" / "config.toml"
    seed_venv(tmp_prefix, warm_prefix)
    installed = run(["bash", "install.sh", "--prefix", str(tmp_prefix), "-y"], env=env)
    assert installed.returncode == 0, installed.stderr
    assert entry.is_file()

    dry = run(["bash", "install.sh", "--prefix", str(tmp_prefix), "-y", "--dry-run",
               "--no-autostart"], env=env)
    assert dry.returncode == 0, dry.stderr
    assert "would remove" in dry.stdout
    assert entry.is_file(), "a dry run removes nothing"

    opted_out = run(["bash", "install.sh", "--prefix", str(tmp_prefix), "-y", "--no-autostart"],
                    env=env)
    assert opted_out.returncode == 0, opted_out.stderr
    assert not entry.exists()
    assert "autostart = false" in config.read_text()


def test_a_config_the_app_cannot_read_stops_the_install_with_a_message(tmp_prefix, warm_prefix,
                                                                       fake_release):
    """The record is written through the app's own loader, so a hand-broken config.toml is the
    installer's error line and exit 1, and the file is left as the user wrote it."""
    good = fake_release("good")
    env = {"UTTER_MODEL_URL": good, "UTTER_VOICES_URL": good}
    config = tmp_prefix / "config" / "utter" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("this is not [ valid toml")
    seed_venv(tmp_prefix, warm_prefix)

    result = run(["bash", "install.sh", "--prefix", str(tmp_prefix), "-y", "--no-autostart"],
                 env=env)
    assert result.returncode == 1
    assert "cannot record the install" in result.stderr
    assert "Traceback" not in result.stderr
    assert config.read_text() == "this is not [ valid toml"


@pytest.mark.skipif(shutil.which("kwriteconfig6") is None, reason="needs Plasma's kwriteconfig6")
def test_the_kwin_rule_is_added_once_and_removed_without_touching_other_rules(
        tmp_prefix, warm_prefix, fake_release):
    """The rule stops KWin handing the controller the keyboard; it shares kwinrulesrc with the
    user's own rules, so install and uninstall must leave every other rule as it was."""
    good = fake_release("good")
    env = {"UTTER_MODEL_URL": good, "UTTER_VOICES_URL": good}
    rules = tmp_prefix / "config" / "kwinrulesrc"
    rules.parent.mkdir(parents=True)
    rules.write_text("[General]\ncount=1\nrules=mine\n\n[mine]\nDescription=My rule\nwmclass=foo\n")
    seed_venv(tmp_prefix, warm_prefix)

    def read(group, key):
        return subprocess.run(
            ["kreadconfig6", "--file", str(rules), "--group", group, "--key", key],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    for _ in range(2):  # a re-install must not list the rule twice
        installed = run(["bash", "install.sh", "--prefix", str(tmp_prefix), "-y",
                         "--no-autostart"], env=env)
        assert installed.returncode == 0, installed.stderr
    assert read("General", "rules") == "mine,utter-controller"
    assert read("General", "count") == "2"
    assert read("utter-controller", "wmclass") == "utter"
    assert read("utter-controller", "acceptfocus") == "false"
    assert read("mine", "wmclass") == "foo"

    removed = run(["bash", "install.sh", "--prefix", str(tmp_prefix), "--uninstall"])
    assert removed.returncode == 0, removed.stderr
    assert read("General", "rules") == "mine"
    assert read("General", "count") == "1"
    assert read("utter-controller", "wmclass") == ""
    assert read("mine", "wmclass") == "foo"
