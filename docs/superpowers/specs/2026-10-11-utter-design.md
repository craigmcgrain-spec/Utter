# Utter — Design Spec

**Date:** 2026-10-11
**Status:** approved for planning
**Source spec:** `/home/mcgrain/Documents/Obsidian/Debugging/Debugging/Utter Spec.md`
**Repo:** `https://github.com/craigmcgrain-spec/Utter` (empty; initialised at this directory, `Projects/Text-to-Speech`)

---

## 1. Summary

Utter is a CPU-only, air-gapped Linux text-to-speech utility. The user selects text with the
mouse in any application, presses a global hotkey, and hears it spoken by a local quantised
ONNX build of Kokoro 82M. A system-tray icon and a small floating controller provide
play/pause, stop, voice selection and speed control.

**Success criteria**

1. Mouse-select text in any app → hotkey → audio, with no network activity at runtime.
2. Zero GPU/CUDA/Vulkan dependencies; CPU execution on 2–4 threads.
3. Idle CPU ≈ 0%; active RSS within 120–200 MB.
4. Survives absent selections, dead audio devices, corrupt models, and declined hotkey bindings
   without crashing or hanging.
5. `install.sh` is the only component that touches the network, and only after explicit consent.

**Non-goals (v1)**

- AT-SPI access to read the focused widget without a selection (the source spec permits
  "Primary Selection **or** AT-SPI"; Primary Selection is chosen).
- Right-click context-menu integration for arbitrary applications — not implementable on Linux.
- GPU/CUDA/ROCm execution providers.
- AppImage distribution.
- Word-level highlight/scrubbing; queue management UI.
- Verified quality for non-English voices (they work through espeak-ng; only `en-us`/`en-gb` are
  validated by this spec's verification plan).
- `continuous` overlapping-window synthesis and phoneme timing output (available in the library
  if a later feature needs word highlighting).

---

## 2. Verified facts

Measured on the target machine (Fedora 44, KDE Plasma 6, Wayland, PipeWire) or read from primary
upstream sources. These are constraints the design depends on.

| Fact | Evidence |
|---|---|
| System Python is 3.14.8; `kokoro-onnx` 0.6.1 declares `>=3.10,<3.14` | PyPI metadata, sdist `pyproject.toml` |
| `uv` 0.12.22 present; `cpython-3.12.15` downloadable | `uv python list` |
| `onnxruntime` 1.31.0 ships manylinux x86_64 wheels for cp312 and cp314 | PyPI |
| PyQt6 6.11.0 ships `cp310-abi3` wheels; `PyQt6-Qt6` 6.11.2 bundles `plugins/platforms/libqwayland.so` and `libQt6DBus.so.6` | wheel `RECORD` inspection |
| `kokoro-v1.0.int8.onnx` = 88.1 MB and `voices-v1.0.bin` = 26.9 MB in GitHub release `model-files-v1.0` of `thewh1teagle/kokoro-onnx` | GitHub releases API |
| `kokoro-onnx` 0.6.1 is MIT licensed; runtime deps are `onnxruntime`, `espeakng-loader`, `phonemizer`, `numpy` | sdist `pyproject.toml` |
| `kokoro-onnx` bundles a 114-entry phoneme vocab in `src/kokoro_onnx/config.json`, prefers a vocab embedded in the model (`kokoro_config` ONNX metadata), and `espeakng-loader` ships the espeak-ng shared library and data | sdist `config.json`, `session.py`, `tokenizer.py` |
| `Kokoro.from_session(session, voices_path)` accepts a caller-built `InferenceSession`, so CPU thread counts and provider selection stay under our control | sdist `__init__.py`, `examples/with_session.py` |
| `Kokoro.create(..., is_phonemes=True)` accepts caller-supplied phonemes — the G2P seam | sdist `__init__.py`, `examples/with_phonemes.py` |
| `MAX_PHONEME_LENGTH = 510`, `SAMPLE_RATE = 24000`; phoneme batches are balanced and pauses/trimming are applied per batch | sdist `config.py`, `chunker.py` |
| `libportaudio.so.2`, `libpulse.so.0`, PipeWire present → `sounddevice` output works | `ldconfig -p` |
| `espeak-ng` binary and `libespeak` are present, but are not required: `espeakng-loader` bundles both | `which`, `ldconfig`, sdist `tokenizer.py` |
| `org.freedesktop.portal.GlobalShortcuts` v2 is live (`xdg-desktop-portal` 1.22.1, `xdg-desktop-portal-kde` 6.7.5): `CreateSession`/`BindShortcuts`/`ListShortcuts`/`ConfigureShortcuts`, signals `Activated`/`Deactivated`/`ShortcutsChanged` | `busctl --user introspect org.freedesktop.portal.Desktop` |
| Trigger syntax is `MOD+…+key` over `CTRL ALT SHIFT NUM LOGO` (xkbcommon names) | XDG Shortcuts Specification 0.1 |
| Unsandboxed apps have no portal app-id unless `org.freedesktop.host.portal.Registry.Register()` is called **before any other portal call**, with an app-id matching a `.desktop` basename; the app-id otherwise comes from the systemd unit name | upstream portal docs; GNOME blog "Using Portals with unsandboxed apps" |
| `jeepney` 0.9.0 is pure Python with no runtime dependencies | PyPI |
| The user is not in the `input` group; `/dev/input` hotkey reading would need privilege escalation | `groups`, `ls -l /dev/input` |

Known platform limitation: Wayland forbids client-side positioning of top-level windows
(`move()` is ignored). This is documented, not worked around.

`[INFERENCE]` marked for Phase 1 measurement: one 510-phoneme batch is roughly 30 s of audio
(≈17 phonemes/s of English speech), so relying on the library's whole-text streaming would put
time-to-first-sound in the seconds. §7.3 therefore sizes synthesis units itself and Phase 1
measures the result; the source spec's "≤150 ms" is treated as a target to measure, not a claim.

---

## 3. Decisions (approved during brainstorming)

| # | Decision |
|---|---|
| D1 | Trigger: mouse-select text, then a global hotkey. Primary Selection is the text source. |
| D2 | Topology **A**: one Qt process holding the warm model, plus a one-file Unix-socket control channel for the `utter` CLI. No D-Bus daemon, no process split. |
| D3 | Text source: Primary Selection, falling back to the clipboard when Primary is empty/stale. |
| D4 | Hotkeys: `org.freedesktop.portal.GlobalShortcuts` on Wayland, `python-xlib` `XGrabKey` on X11, `none` elsewhere. No `/dev/input`, no root, no fake. |
| D5 | Packaging: uv-managed venv + `.desktop` launcher. Not AppImage. |
| D6 | Model: `kokoro-v1.0.int8.onnx` + `voices-v1.0.bin`, SHA-256 pinned, fetched by `install.sh` only. |
| D7 | G2P: espeak-ng phonemisation through `kokoro_onnx.tokenizer.Tokenizer` in v1, behind a `Phonemizer` seam using `is_phonemes=True`. `misaki[en]` is a candidate upgrade decided by listening to a comparison clip produced in Phase 1. |
| D8 | Build sequence: one plan, three phase-gated increments (engine → UI → hotkey/installer), each verified end-to-end before the next. |
| D9 | Inference goes through `kokoro-onnx` 0.6.1 (pinned) rather than a hand-rolled tokenizer/inference path. |
| D10 | Synthesis is `Kokoro.create()` per Utter-sized text unit in our own worker thread — **not** `create_stream` (see §7.3). |
| D11 | Overlay is kept, with the Wayland positioning caveat documented; the tray menu duplicates every control. |

---

## 4. Architecture

```
                    mouse selection (Primary Selection)
                                │
                    global hotkey (portal | XGrabKey)
                                │
                                ▼
   ┌─────────────────────── utter (single PyQt6 process) ───────────────────────┐
   │  hotkey.py ──► capture.py ──► tts/engine.py (worker thread)                │
   │                                    │  chunk.py → phonemes.py → model.py    │
   │                                    ▼                                       │
   │  ui/tray.py, ui/overlay.py ◄── tts/audio.py (sd.OutputStream, 24 kHz)      │
   │  control.py ── UNIX socket $XDG_RUNTIME_DIR/utter.sock ◄── cli.py          │
   └────────────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
                PipeWire / PulseAudio (via PortAudio)
```

### 4.1 Module map

```
pyproject.toml            project metadata, deps, [project.scripts] utter
.python-version           3.12
install.sh                CLI installer / uninstaller (the only network code)
README.md  LICENSE        Apache-2.0 (Utter's own code)
src/utter/
  __main__.py             entry: portal Register → QGuiApplication → ui/app.py
  paths.py                XDG directories, socket path, log path, model paths
  config.py               config dataclasses; tomllib read, tomli-w write (atomic)
  capture.py              selection reading + sanitisation
  hotkey.py               HotkeyBackend protocol; PortalHotkey; X11Hotkey; select_backend()
  control.py              socket server + client, JSON-lines protocol
  cli.py                  argparse entry point
  tts/
    chunk.py              text → synthesis units (sizing rules in §7.3)
    phonemes.py           Phonemizer protocol; EspeakPhonemizer (wraps the library Tokenizer)
    model.py              model paths, SHA-256 check, session options, lazy load, idle unload
    engine.py             SynthService: unit queue, worker thread, generation counter, speed
    audio.py              Player: output stream, frame buffer, play/pause/stop
  ui/
    app.py                wiring, lifecycle, single-instance delegation
    tray.py               QSystemTrayIcon and menu
    overlay.py            floating micro-controller
    notify.py             tray notification helpers
tests/                    unit and integration tests
docs/superpowers/specs/   this document
```

Each module has one purpose and a testable interface. `tts/*` never imports `ui/*` or PyQt;
`ui/*` never imports `onnxruntime`. The engine is therefore testable headless and
`utter speak` works without a GUI.

### 4.2 Data flow

```
hotkey Activated | KeyPress
  └─ capture.read_selection() → None ⇒ notify("No text selected"); return
       └─ SynthService.speak(text)                        (Qt main thread → worker)
            generation += 1
            pending = text                                latest wins, depth 1
            Player.stop(flush=True)                        audible stop is immediate
            worker (single thread) loops:
              take pending                                 wait when idle
              units = chunk.split_units(text)
              per unit:
                if generation != mine: drop it, take new pending (or exit)
                audio = kokoro.create(unit, voice, speed, lang, trim=True)   float32, 24 kHz
                queue.put((audio, pause_after_unit), timeout=0.1)   bounded, maxlen 8
            Player drains the queue and writes frames to PortAudio
  └─ overlay.show() + tray state update
```

`generation` is a monotonic integer owned by `SynthService`. The worker checks it before each unit
and on every queue `put` timeout, so a new hotkey press supersedes the previous text as soon as the
in-flight unit returns. A unit already inside `Kokoro.create()` cannot be interrupted — see §7.3
for the sizing rule and the responsiveness invariant that bound that cost.

---

## 5. Text capture

| Session | Primary attempt | Fallback |
|---|---|---|
| Wayland | `wl-paste --primary --no-newline` | `QClipboard.Clipboard` text |
| X11 | `QClipboard.Selection` (Qt X11 selection mode) | `QClipboard.Clipboard` text |

Rationale for the fallback: Electron, VS Code and Wayland-native Firefox frequently do not
publish a Primary Selection, so a Primary-only design looks broken in those applications.

Sanitisation: strip NUL bytes, strip the trailing newline, collapse runs of whitespace, reject
empty or whitespace-only selections silently (no notification, no state change), and refuse
selections longer than `speech.max_chars` (default 20 000) with a notification instead of
blocking the engine for minutes.

`capture.read_selection()` accepts injected command runners so the fallback matrix is unit-testable
without a live display. AT-SPI is out of scope for v1; the single function boundary allows an
`AtspiCapture` later without touching callers.

---

## 6. Hotkeys

`select_backend(config)`:

| Condition | Backend |
|---|---|
| `hotkey.backend = "none"` | none |
| `hotkey.backend = "portal"` | PortalHotkey |
| `hotkey.backend = "x11"` | X11Hotkey |
| `auto` with `XDG_SESSION_TYPE=wayland` and the portal interface present | PortalHotkey |
| `auto` with `XDG_SESSION_TYPE=x11` | X11Hotkey |
| `auto` otherwise | none, with instructions |

### 6.1 PortalHotkey (Wayland)

Implemented with `jeepney` (blocking calls), used before Qt initialises.

1. `org.freedesktop.host.portal.Registry.Register("utter", {})` on
   `org.freedesktop.portal.Desktop` `/org/freedesktop/host/portal/Registry`. This must be the
   process's first portal call; a later call fails. Failure is logged and non-fatal.
2. `GlobalShortcuts.CreateSession({handle_token, session_handle_token})`; await
   `Request::Response` for `session_handle`.
3. `GlobalShortcuts.BindShortcuts(session, shortcuts, "", {handle_token})` with
   `[("speak", {"description": "Speak selected text", "preferred_trigger": "LOGO+ALT+s"}),
   ("stop", {"description": "Stop speaking", "preferred_trigger": "LOGO+ALT+x"}),
   ("pause", {"description": "Pause or resume", "preferred_trigger": "LOGO+ALT+p"})]`.
   The desktop shows a one-time configuration dialog. An empty returned set means the user
   declined: notify once and print the CLI-binding instructions. Rebinding is available later
   through `utter hotkey --show` → `ConfigureShortcuts`.
4. Dispatch `Activated(session, shortcut_id, …)` → `speak` / `stop` / `pause`.
5. On `NameOwnerChanged` for `org.freedesktop.portal.Desktop`, repeat step 1 and `ListShortcuts`
   to recover bindings after a portal restart.

The app-id must match a `.desktop` basename, so `install.sh` installs
`~/.local/share/applications/utter.desktop` before the first bind. `utter doctor` reports the
app-id actually in use.

### 6.2 X11Hotkey

`python-xlib` `XGrabKey` on the root window for each configured trigger, with a dedicated thread
reading `next_event()`; re-grab on `MappingNotify`; `ungrab_key` on shutdown. `auto` never selects
this backend on Wayland.

### 6.3 Unsupported sessions

GNOME on Wayland without the portal implementation: no binding is possible without a shell
extension. Utter does not fake it — it notifies once, prints the exact
"Settings → Keyboard → Custom Shortcuts → `utter speak`" steps, and keeps working through the tray
and CLI. `utter speak` exists on every session, so a user-bound DE shortcut is always available.

---

## 7. Engine and playback

### 7.1 Model, session and inference library

- Files under `$XDG_DATA_HOME/utter/models/`: `kokoro-v1.0.int8.onnx` (88.1 MB) and
  `voices-v1.0.bin` (26.9 MB).
- `tts/model.py` builds the session itself so thread and provider settings are ours:

```python
opts = ort.SessionOptions()
opts.intra_op_num_threads = max(1, min(4, os.cpu_count() // 2))
opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
session = ort.InferenceSession(model_path, sess_options=opts,
                               providers=["CPUExecutionProvider"])
kokoro = Kokoro.from_session(session, voices_path)
```

- `CPUExecutionProvider` is passed explicitly, so installing an accelerated `onnxruntime`
  distribution can never silently pull in a GPU provider (source spec §5, NFR 1).
- Voices are read from the `.npz` by the library; `voice_names()` returns
  `kokoro.voices.files` for the tray menu and `utter voices`.
- Lazy load on first `speak`. After `general.model_idle_unload_minutes` (default 10) without
  speech, the worker drops the `Kokoro` object and session so idle RSS returns to the Qt
  baseline; the next `speak` reloads and logs the load cost.
- Speech parameters validated by the library: `0.5 <= speed <= 2.0`. Utter's slider range
  (0.75–2.0) is inside it.
- `model.py` contains no download code. The installer fetches; the runtime verifies SHA-256
  against the digests recorded at install time and refuses to load on mismatch.

### 7.2 Phonemisation seam (D7)

`tts/phonemes.py` defines:

```python
class Phonemizer(Protocol):
    def phonemize(self, text: str, lang: str) -> str: ...
```

`EspeakPhonemizer` delegates to `kokoro_onnx.tokenizer.Tokenizer.phonemize` (espeak-ng with
`preserve_punctuation=True, with_stress=True`, filtered to the 114-entry vocab). The engine takes
one of two paths:

- **Default (`speech.phonemizer = "espeak"`, or no phonemizer configured):** call
  `kokoro.create(unit, voice=voice, speed=speed, lang=lang, trim=True)`, letting the library
  phonemise internally. This is the same espeak-ng code path as `EspeakPhonemizer`, with one less
  indirection, so the default runtime behaviour does not depend on our wrapper.
- **Alternative (`speech.phonemizer = "misaki"`):** call
  `kokoro.create(phonemizer.phonemize(unit, lang), voice=voice, speed=speed, lang=lang,
  is_phonemes=True)`.

`EspeakPhonemizer` is therefore the reference implementation of the protocol: it backs the unit
tests for the seam and produces the espeak half of the Phase-1 comparison clip. Swapping in
`misaki[en]` becomes one config value plus one class, and the choice is made by ear rather than by
claim.

### 7.3 Synthesis units (D10)

Synthesis is `Kokoro.create()` per unit, from our worker thread. `create_stream` is not used:
it batches up to `MAX_PHONEME_LENGTH = 510` phonemes per chunk, and the first chunk is only
available after that much audio has been synthesised, which conflicts with "audio starts fast
after the hotkey".

Unit sizing rules, in `tts/chunk.py`:

1. Split the text on sentence terminators (`.`, `!`, `?`, `…`) and newlines.
2. The **first unit** is cut short — at the first clause mark (`,`, `;`, `:`) or 60 characters,
   whichever comes first — so the first audio can start as early as the model allows.
3. Remaining units are sentences, further split on clause marks and then hard-wrapped at 200
   characters.
4. Fragments shorter than three words are merged into the neighbouring unit to avoid choppy
   prosody at the joins.
5. Input above `speech.max_chars` is refused before any synthesis begins.

The 200-character cap in rule 3 exists to bound the one thing the design cannot interrupt.
`InferenceSession.run()` cannot be cancelled mid-call, so the unit in flight when the user stops
or presses the hotkey again is the worst case for both CPU waste and responsiveness. A 200-character
unit is roughly 35 words of speech; Phase 1 measures the real-time factor for the pinned int8 model
and reports the resulting worst case, targeting ≤1 s.

**Responsiveness invariant:** `SynthService` keeps a single worker thread and a latest-wins
pending-request slot (depth 1). `speak()` replaces any pending request and increments the
generation, so a new hotkey press is honoured as soon as the in-flight unit returns — never after
the whole previous text. If Phase 1 measures worse than the ≤1 s target, the knob is the
200-character cap in rule 3, not the thread topology.

The worker inserts `speech.sentence_pause` (default 0.25 s) of silence between units, and passes
`speech.sentence_pause` / `speech.clause_pause` to `Kokoro.create` so intra-unit pauses match.

### 7.4 Playback

- `sounddevice.OutputStream(samplerate=24000, channels=1, dtype='float32')`, push mode.
- The player thread pulls `(audio, pause)` pairs from the bounded queue (maxlen 8) and writes
  frames. A full queue blocks the worker, giving natural backpressure.
- **Pause** stops the player thread writing frames while leaving the stream open; PortAudio
  underruns to silence. Synthesis continues until the queue fills, then blocks. **Resume**
  continues from the next unwritten frame with no re-synthesis.
- **Stop** silences audio immediately: increment the generation, clear the queue, flush the
  stream, hide the overlay. The worker's in-flight unit finishes in the background and its result
  is discarded, so the only cost is CPU — bounded by one unit per §7.3.
- **Speed** is the library's duration-scaling parameter (pitch preserved), not resampling, so it
  applies per unit. A slider change takes effect on release and applies from the next synthesised
  unit; when idle it applies immediately. The overlay tooltip states this.
- Audio-device loss (route change, suspend/resume) is caught, the stream is reopened once, and on
  repeated failure playback stops cleanly with a notification.

### 7.5 Voices and language

`voices-v1.0.bin` is loaded with the model and released on idle unload. The language phoneme set
is derived from the voice-id prefix (`a`=en-us, `b`=en-gb, `e`=es, `f`=fr, `h`=hi, `i`=it,
`j`=ja, `p`=pt-br, `z`=zh), overridable with `speech.lang`. Default voice: `af_heart`. Only
`en-us`/`en-gb` are verified by this spec.

---

## 8. Configuration, state and CLI

### 8.1 Paths

| Purpose | Path |
|---|---|
| Config | `$XDG_CONFIG_HOME/utter/config.toml` |
| Log | `$XDG_STATE_HOME/utter/utter.log` (RotatingFileHandler, 1 MiB × 3) |
| Models | `$XDG_DATA_HOME/utter/models/` |
| Venv | `$XDG_DATA_HOME/utter/venv/` |
| Control socket | `$XDG_RUNTIME_DIR/utter.sock` (mode 0600) |
| Launcher | `~/.local/share/applications/utter.desktop` |
| Autostart | `~/.config/autostart/utter.desktop` |

### 8.2 Config file

```toml
[general]
autostart = false
model_idle_unload_minutes = 10
log_level = "info"

[speech]
voice = "af_heart"
speed = 1.0
lang = "en-us"
phonemizer = "espeak"   # espeak | misaki (see §7.2)
max_chars = 20000
sentence_pause = 0.25
clause_pause = 0.10

[hotkey]
backend = "auto"        # auto | portal | x11 | none
speak = "LOGO+ALT+s"
stop = "LOGO+ALT+x"
pause = "LOGO+ALT+p"

[ui]
show_overlay = true
overlay_pos = [0, 0]    # honoured on X11 only

[install]
consent_given = false
model_sha256 = ""
voices_sha256 = ""
```

Written atomically (temp file + `rename`). Precedence: built-in defaults < `config.toml` <
`UTTER_*` environment variables < CLI flags.

### 8.3 CLI

| Command | Behaviour |
|---|---|
| `utter` | Start the tray app. If a live socket already owns `utter.sock`, delegate "show" and exit 0. |
| `utter speak [TEXT]` | Delegate to the running instance; with no instance, run a cold in-process engine (logged as a cold start). `--text` and `--file` also accepted. |
| `utter stop` / `pause` / `resume` / `status` | Control socket. `status` prints JSON: speaking, paused, current unit, voice, speed, generation. |
| `utter voices [--json]` | List voices from the voice pack, marking the active one. |
| `utter doctor` | Resolved config, model paths and hashes, hotkey backend and portal app-id, audio device, last latency samples; non-zero exit when something is actually wrong. |
| `utter hotkey --show` | Re-open the portal binding dialog, or print bind instructions when no portal is available. |

The socket protocol is JSON-lines: one request object per line, one response object per line, plus
`{"event": …}` lines for state changes. A stale socket file is detected by connect-probe and
unlinked before rebinding.

---

## 9. Error handling and the air-gap guarantee

- Worker exceptions are caught per unit: log the traceback, notify `Speech failed: <reason>`, keep
  the app alive, and restart the worker on the next `speak`.
- Missing or hash-mismatched model → notification plus a `doctor` diagnosis; never a download.
- Empty selection → one tray message, no modal, no audio state change.
- Audio failure → one reopen attempt, then a clean stop with a notification.
- Declined hotkey binding → notification plus CLI-bind instructions; tray and CLI keep working.
- Single-instance conflicts (stale socket, foreign owner of the path) are resolved by
  connect-probe, not PID files.
- Logs record the resolved config once at startup and one line per utterance (unit count, audio
  duration, synthesis time, time-to-first-frame). This is the source of the numbers in §11. No
  usage data leaves the machine.

**Air gap, structurally enforced:** all download code lives in `install.sh`; `src/utter` contains
no HTTP client. Phase-3 verification greps `src/utter` for `urllib|requests|httpx|socket.create_connection`
(only AF_UNIX uses may match) and runs the app under `strace -f -e trace=network` while speaking,
confirming zero non-unix socket syscalls.

---

## 10. Installer and packaging

`install.sh`, bash, `set -euo pipefail`. Flags: `-y|--yes`, `--no-autostart`, `--dry-run`,
`--uninstall [--purge]`, `--prefix DIR` (redirects all XDG roots for testing).

1. **Preflight report** — session type, desktop, compositor, audio server; presence of `uv` and
   `wl-clipboard`; prints the exact `sudo dnf install …` line for anything missing. `espeak-ng` is
   reported as optional, since `espeakng-loader` bundles the library and data. The script never
   invokes `sudo` itself.
2. **Consent gate** — prints the download ledger (model 88.1 MB + voices 26.9 MB + CPython 3.12
   ≈30 MB + wheels ≈250 MB; ≈400 MB total) and requires `y`; the default answer is `n`. `--yes`
   skips the prompt. This is source spec §Build.1.
3. **Install** — `uv venv --python 3.12 $XDG_DATA_HOME/utter/venv`, then install this package into
   it with `kokoro-onnx==0.6.1` pinned (via the project's `pyproject.toml`).
4. **Fetch** — `curl -fL -C -` from the pinned release URLs into a temp directory, `sha256sum -c`
   against digests pinned in the script, atomic move into `models/`; on mismatch delete and abort
   printing expected vs actual. Digests are recorded in `config.toml`. Exactly two files are
   fetched: the model and the voice pack (no vocab file — see §7.2).
5. **Desktop integration** — `hicolor/scalable/apps/utter.svg`, `utter.desktop` with
   `Exec=<venv>/bin/utter`, `Icon=utter`, `Categories=Utility;Accessibility;`,
   `Keywords=tts;speech;`, `Terminal=false`, best-effort `update-desktop-database`.
6. **Autostart** — prompts `Start Utter automatically at login? [Y/n]` and writes
   `~/.config/autostart/utter.desktop`; also togglable at runtime from the tray, which reads and
   writes the same file.
7. **Shim** — symlink `~/.local/bin/utter`; prints a PATH note when `~/.local/bin` is not on PATH.
8. **Post-install** — explains the one-time portal hotkey dialog, the GNOME-without-portal
   instructions, and the uninstall command.
9. **`--uninstall`** — stop a running instance, then remove venv, models, launcher, icon,
   autostart entry and shim; keep `config.toml` unless `--purge`. Re-running the installer is
   idempotent.

Distribution is a git checkout plus `install.sh`; the installer creates the venv, never the user.
No AppImage: bundling Qt's Wayland plugin, the portal client and the model would add size and
failure modes without capability.

---

## 11. Verification plan

Verification is per-phase and runs real code, not tests alone.

**Phase 1 — engine**
- Unit: unit sizing (first-unit clause cut, sentence split, >200-char split, small-fragment
  merge, `max_chars` refusal); capture fallback matrix with injected runners; config round-trip and
  precedence; engine generation-cancellation with a fake synthesizer; latest-wins pending request
  (depth 1) superseeding an in-flight text; player state machine (stop/pause/resume/drain) with a
  fake stream.
- Real smoke, on CPU: `utter speak --text <paragraph>`. Record model load time, per-unit
  synthesis time, real-time factor, **time-to-first-frame**, **worst-case responsiveness**
  (time from a superseding `speak()` to the first frame of the new text — the §7.3 invariant,
  target ≤1 s), and peak RSS (budget ≤200 MB active). Write a WAV and verify sample rate, duration
  and non-silence out-of-band.
- Confirm against the pinned int8 model: whether it carries `kokoro_config` vocab metadata and a
  `duration` output; that unknown phonemes are dropped rather than fatal; that `speed` is a float
  input (not the v1.1-zh integer variant).
- Deliverable: an espeak-vs-misaki comparison clip (from a throwaway venv) for the D7 decision.
  The reported latency numbers are measurements, not the source spec's ≤150 ms claim.

**Phase 2 — UI**
- Launch the real app on the KDE Wayland session; confirm the StatusNotifierItem registers
  (`busctl --user`); screenshot the overlay; drive pause → resume → stop mid-sentence while
  observing queue and stream transitions.
- Run `utter speak` from a terminal while the app runs, proving socket delegation and no second
  tray instance.

**Phase 3 — hotkey and installer**
- `install.sh --dry-run`, then a real install into a temporary XDG root, then a real install.
- Trigger the portal `BindShortcuts` dialog; confirm `Activated` fires and audio plays for
  mouse-selected text in Kate and Firefox.
- Exercise the X11 path under `Xephyr` (XGrabKey cannot be tested headless otherwise).
- Confirm zero non-unix network syscalls under `strace` while speaking.
- `--uninstall` verified by diffing `find` over the XDG roots before and after.

**Human-only acceptance steps** (cannot be automated): pressing the real hotkey in the live
session after granting the portal dialog, and judging voice quality.

**Test hygiene:** tests run against a temporary XDG root via `UTTER_*` overrides and never touch
the user's real `~/.config`, `~/.local/share` or `~/.local/bin`. Tests assert behaviour —
boundaries, invariants, transitions, error paths — not wiring, incidental defaults or source text.

---

## 12. Phases

| Phase | Deliverable | Exit condition |
|---|---|---|
| 1 | `tts/` engine + `capture.py` + config + `utter speak` CLI | A paragraph is synthesised on CPU from the CLI; latency and RSS measured; comparison clip produced |
| 2 | Tray icon, overlay, control socket, full CLI | Pause/resume/stop observed on real audio; tray visible on Wayland; CLI delegates to the warm process |
| 3 | Hotkeys, installer, desktop integration, autostart | Hotkey speaks a mouse selection in a real app; `--uninstall` leaves no residue |

Each phase must run end-to-end before the next begins.

---

## 13. Risks

| Risk | Mitigation |
|---|---|
| espeak-ng prosody is audibly worse than misaki | D7 seam (`is_phonemes=True`) + Phase-1 comparison clip; `misaki[en]` is a contained swap (+≈150 MB deps) |
| First-unit clause cut sounds discontinuous against the next unit | Small pause between units; if audible, rule 2 falls back to first-unit = first sentence — decided by listening in Phase 1 |
| Time-to-first-sound misses the 150 ms target | Measured in Phase 1 and reported; the mechanism (short first unit) is in the design, the residual cost is the model's real-time factor |
| Worst-case responsiveness (a superseding hotkey press waits for the in-flight unit) exceeds 1 s | §7.3 invariant with a measured target; the knob is the 200-character unit cap, not the thread topology |
| A very long word run with no clause or sentence mark forces an oversized unit | Hard-wrap at 200 characters inside rule 3 bounds it regardless of punctuation |
| Pinning `kokoro-onnx==0.6.1` blocks a future Python upgrade | It is the only Python-version constraint (`<3.14`); upgrade path is the library's own release cadence |
| Portal app-id mismatch when launched from a bare terminal | Pre-Qt `Register`, launch from `.desktop`/autostart, and `doctor` reporting the app-id in use |
| `XGrabKey` conflicts with existing DE shortcuts | Configurable triggers; grab failure logged and reported by `doctor`; the CLI path is always available |
| Wayland overlay lands wherever the compositor puts it | Tray menu carries identical controls; limitation documented in the README and the overlay tooltip |
| First `speak` after an idle unload pays the model load cost | Configurable idle window; the load time is logged so the trade-off is visible |
| int8 quality loss versus fp16 | Both files are pinned by URL in the installer; the fp16 swap is a flag plus an idle reload |

## 14. Licensing and attribution

Utter's own code is Apache-2.0. Kokoro-82M and its ONNX export are Apache-2.0; `kokoro-onnx` is
MIT; espeak-ng is GPL-3.0 and is used as a bundled shared library by `espeakng-loader` (no
linking by Utter's code). The README credits `hexgrad/Kokoro-82M` (model),
`thewh1teagle/kokoro-onnx` (ONNX export, voice pack, Python bindings) and `espeak-ng` (G2P).
