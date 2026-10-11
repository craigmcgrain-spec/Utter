# Utter — Design Spec

**Date:** 2026-10-11
**Status:** approved for planning; §2/§7/§13 amended with measurements from the pre-plan probes (commit history)
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
2. Zero GPU/CUDA/Vulkan dependencies; CPU execution on 4 threads.
3. Idle CPU ≈ 0%; steady-state RSS ≤ 320 MiB in the typical case. Measured steady state on the
   pinned artifacts is 247 MiB with the CPU memory arena disabled (§2.3). Phase 1's end-to-end runs
   measured 198–367 MiB over nine real playback runs (six sampled at 0.2 s and three at 0.25 s;
   median ≈240 MiB, one run above 320 MiB), so the criterion is stated as a distribution rather
   than a maximum: median ≤320 MiB, with the tail reported (one run in nine above it) and peaks
   (465–943 MiB session-wide, allocator-dominated, measured over the render and playback runs)
   reported separately. **The source spec's "120–200 MB" is not
   achievable with this stack** — the model plus ONNX Runtime floor is 191 MiB before any audio is
   produced. This is a recorded deviation, not a target we expect to miss silently.
4. First sound after the hotkey ≤ 0.6 s warm in the typical case. The Phase 1 end-to-end runs
   measured **1.00–1.11 s cold** (central run 1.05 s, of which ≈0.49 s is the model load) and
   **0.52–0.64 s warm over nine runs, typically ≈0.55 s**, against the original 0.5 s form of this
   criterion — a recorded miss of
   about 10%, so it is restated as a distribution with the tail reported and the cold figure stated
   separately because the model load is unavoidable on a first utterance. The residual is the first
   unit's own synthesis: §7.3's 60-character first-unit cap could be lowered to 40 characters,
   which the probe's numbers (§2.3: 39 characters → 0.45 s) say would land at ≈0.45 s, but that
   spends prosody — the cut then often lands at a word boundary mid-clause rather than at a clause
   mark — so it is left as the lever for Phase 1's listening test rather than taken unilaterally.
   The source spec's "≤150 ms" is not attainable for meaningful text on this CPU: 150 ms buys
   roughly 12 phonemes, about two words.
5. Survives absent selections, dead audio devices, corrupt models, and declined hotkey bindings
   without crashing or hanging.
6. `install.sh` is the only component that touches the network, and only after explicit consent.

**Non-goals (v1)**

- AT-SPI access to read the focused widget without a selection (the source spec permits
  "Primary Selection **or** AT-SPI"; Primary Selection is chosen).
- Right-click context-menu integration for arbitrary applications — not implementable on Linux.
- GPU/CUDA/ROCm execution providers.
- AppImage distribution.
- Word-level highlight/scrubbing. The pinned export has no duration output, so phoneme timings and
  the library's `continuous` overlapping-window mode are unavailable (measured, §2.3).
- Verified quality for non-English voices (they work through espeak-ng; only `en-us`/`en-gb` are
  validated by this spec's verification plan).

---

## 2. Verified facts

### 2.1 Environment

Measured on the target machine (Fedora 44, KDE Plasma 6, Wayland, PipeWire, 16 CPUs) or read from
primary upstream sources.

| Fact | Evidence |
|---|---|
| System Python is 3.14.8; `kokoro-onnx` 0.6.1 declares `>=3.10,<3.14` | PyPI metadata, sdist `pyproject.toml` |
| `uv` 0.12.22 present; `cpython-3.12.15` downloadable | `uv python list` |
| `onnxruntime` 1.31.0 ships manylinux x86_64 wheels for cp312 and cp314 | PyPI |
| PyQt6 6.11.0 ships `cp310-abi3` wheels; `PyQt6-Qt6` 6.11.2 bundles `plugins/platforms/libqwayland.so` and `libQt6DBus.so.6` | wheel `RECORD` inspection |
| `kokoro-onnx` 0.6.1 is MIT licensed; runtime deps are `onnxruntime`, `espeakng-loader`, `phonemizer`, `numpy` | sdist `pyproject.toml` |
| `kokoro-onnx` bundles a 114-entry phoneme vocab in `src/kokoro_onnx/config.json` and `espeakng-loader` ships the espeak-ng shared library and data | sdist `config.json`, `tokenizer.py` |
| `Kokoro.from_session(session, voices_path)` accepts a caller-built `InferenceSession` | used successfully in probe 1 (§2.3) |
| `Kokoro.create(..., is_phonemes=True)` accepts caller-supplied phonemes | exercised in probes 2 and 4 (§2.3) |
| `MAX_PHONEME_LENGTH = 510`, `SAMPLE_RATE = 24000`; phoneme batches are balanced, and pauses are added at batch joins | sdist `config.py`, `chunker.py` |
| `libportaudio.so.2`, `libpulse.so.0`, PipeWire present → `sounddevice` output works | `ldconfig -p` |
| `espeak-ng` binary and `libespeak` are present but not required: `espeakng-loader` bundles both | `which`, `ldconfig`, sdist `tokenizer.py` |
| `org.freedesktop.portal.GlobalShortcuts` v2 is live (`xdg-desktop-portal` 1.22.1, `xdg-desktop-portal-kde` 6.7.5): `CreateSession`/`BindShortcuts`/`ListShortcuts`/`ConfigureShortcuts`, signals `Activated`/`Deactivated`/`ShortcutsChanged` | `busctl --user introspect org.freedesktop.portal.Desktop` |
| Trigger syntax is `MOD+…+key` over `CTRL ALT SHIFT NUM LOGO` (xkbcommon names) | XDG Shortcuts Specification 0.1 |
| Unsandboxed apps have no portal app-id unless `org.freedesktop.host.portal.Registry.Register()` is called **before any other portal call**, with an app-id matching a `.desktop` basename | upstream portal docs; GNOME blog "Using Portals with unsandboxed apps" |
| `jeepney` 0.9.0 is pure Python with no runtime dependencies | PyPI |
| The user is not in the `input` group; `/dev/input` hotkey reading would need privilege escalation | `groups`, `ls -l /dev/input` |

Known platform limitation: Wayland forbids client-side positioning of top-level windows
(`move()` is ignored). This is documented, not worked around.

### 2.2 Pinned artifacts

| Fact | Value |
|---|---|
| Model | `kokoro-v1.0.int8.onnx`, 92 361 271 bytes (88.07 MiB) |
| Model SHA-256 | `6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb` |
| Voices | `voices-v1.0.bin`, 28 214 398 bytes (26.91 MiB), 54 voices |
| Voices SHA-256 | `bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d` |
| Both from | GitHub release `model-files-v1.0` of `thewh1teagle/kokoro-onnx` |
| ONNX inputs | `tokens` int64 `[1, sequence_length]`, `style` float32 `[1, 256]`, `speed` float32 `[1]` |
| ONNX outputs | `audio` only — **no `duration` output** |
| ONNX metadata | **none**: no `kokoro_config`, so no embedded vocab (the bundled 114-entry vocab is used) |
| Load time | 0.47 s for the `InferenceSession` |

### 2.3 Measured behaviour (probe `/tmp/utterprobe`, throwaway, 16 CPUs)

All numbers are from this machine with `CPUExecutionProvider` and `ORT_SEQUENTIAL`.

**Real-time factor scales with unit length.** Synthesis is 2.4–5× faster than playback, and short
units are both cheaper and more CPU-efficient per second of audio (prose with no digits, 4 threads,
arena disabled; best of three runs):

| chars | phonemes | audio | synth | RTF |
|---|---|---|---|---|
| 19 | 22 | 1.30 s | 0.28 s | 0.218 |
| 39 | 46 | 2.30 s | 0.45 s | 0.196 |
| 77 | 86 | 4.57 s | 0.94 s | 0.206 |
| 136 | 148 | 7.94 s | 2.12 s | 0.267 |
| 144 | 157 | 8.47 s | 2.22 s | 0.262 |
| 160 (number-dense) | 224 | 13.65 s | 5.69 s | 0.417 |

The last row is the same sentence used in probe 5: it carries an unusually high phoneme count
because of currency and decimal forms, and its RTF is more than double that of prose of similar
length. Phoneme count, not character count, drives the cost.

**Thread and execution mode.** 4 threads 0.218; 8 threads 0.190 (13% faster, more memory);
all 16 threads 0.236 (worse); `ORT_PARALLEL` 0.222 (no gain). 4 threads is kept, matching the
source spec §4.

**Memory arena.** With `enable_cpu_mem_arena=True`: interpreter baseline 58 MiB → session load
191 MiB → after warm-up 227 MiB → steady state **566 MiB**. With the arena disabled: steady state
**247 MiB** at a 10–18% real-time-factor cost. Disabling the arena is worth ~320 MiB and is
adopted.

**Idle unload does not work.** With the arena enabled, dropping the `InferenceSession` returns RSS
to 126 MiB. With the arena disabled, dropping it returns nothing (247 MiB → 247 MiB) — the
allocator keeps the pages. Since the arena is disabled for memory reasons, an idle-unload feature
would cost a ~0.5 s reload before every first utterance and save no resident memory. **The feature
is therefore removed from the design** rather than shipped inert.

**Sessions must not be recreated.** Creating five sessions in one process reached a peak of
1282 MiB. The session is created once and reused for the process lifetime.

**Pause parameters are inert.** `sentence_pause` and `clause_pause` change the output by 0.00 s for
a 450-character paragraph, because the export has no duration output, so the library can only place
pauses at batch joins. Utter supplies its own inter-unit silence instead (§7.3).

**Speed.** Accepted 0.75/1.0/1.5/2.0, producing 12.76/9.22/5.87/4.89 s for the same sentence;
0.4 and 2.5 raise `ValueError` (the library enforces 0.5–2.0).

**The G2P seam is exact.** Phonemising with our own `Tokenizer` and calling
`create(..., is_phonemes=True)` produced audio bit-identical to letting the library phonemise
internally (9.216 s both ways, `np.allclose` true). Swapping phonemisers is therefore a pure
phoneme-string substitution with no engine change.

**`misaki[en]` works offline once provisioned, at a heavy cost.** Constructing `en.G2P(trf=False)`
shells out to install `en_core_web_sm` (12.2 MiB) on first use — it does this even when nothing asks
it to, and terminates the process with `SystemExit` when that install cannot run, which a plain
`except Exception` does not catch. Provisioned once, the whole path then runs with
`HF_HUB_OFFLINE=1` and no installer reachable, so the air gap survives installation. Its output is
fully consumable through the seam: 190 phonemes for the probe sentence, 100% of them inside the
114-entry vocab, synthesised without error. The cost is a PyTorch dependency (`site-packages/torch`
is on the import path) and resident memory rising from 206 MiB to 845 MiB at construction and
1232 MiB after synthesis — roughly 640 MiB above the espeak path. On disk it costs a 1.2 GB
`torch` tree plus 122 MB of spaCy and the 12.2 MiB model, against 115 MiB total for the entire
espeak-based runtime. Its normalisation is visibly better: the probe sentence's `$0.00` becomes
"zero dollars" where espeak-ng produces "dollar zero.zero".

**espeak-ng's measurable blind spots.** Phonemising a battery of number, currency and abbreviation
forms (§7.6) shows espeak-ng reads prose and most numerics correctly but gets three classes wrong: a
currency symbol produces the wrong order (`$5` → "dollar five", `$0.00` → "dollar zero.zero zero"),
digit-group commas are read as pauses and zeros (`3,000` → "three,zero zero zero"), and a
leading-dot decimal becomes a sentence pause (`.5` → ".five"). Correct as-is: `1.5`, `0.5`, `3000`,
`2024`, `-3`, `+5`, `5%`, `21st`, `99¢` → "ninety nine cents", `12:30`, `Dr.`, `Etc.`, `A/B`.
`num2words`' own currency mode is likewise unusable: `num2words(5, to="currency", currency="USD")`
→ "zero dollars, five cents". §7.6's normaliser fixes all three classes (13 of 13 forms) and leaves
22 correct forms byte-identical.

**Degenerate inputs.** Empty and whitespace-only text raise
`ValueError: Nothing to synthesize`; both must be caught. Punctuation-only text (`...`) yields
0.323 s of near-silence, so units without an alphanumeric character are skipped. Emoji are spoken
as their names (espeak-ng), code fences are read literally, and a 300-character unbroken run
synthesises fine (7.34 s) — no special handling beyond the 80-character wrap.

**Playback backend is verified.** `sounddevice` 0.5.6 / PortAudio V19.7.0-devel, default output on
the PipeWire device. A `samplerate=24000, channels=1, dtype='float32'` push stream opens and reports
42.7 ms of latency; 1.00 s of silence in 512-frame blocks took 0.938 s of wall time, so blocking
writes pace correctly and do not stall. PortAudio transparently resamples 24 kHz to the device rate
(the default device reports 44100). Not writing frames for 0.3 s leaves the stream `active`, and
writing afterwards resumes cleanly — pause by omission is confirmed, which is what §7.4 relies on.
Total time-to-first-sound is therefore synthesis plus ≈43 ms, not a playback-side bottleneck.

**Runtime telemetry hooks exist.** `onnxruntime.disable_telemetry_events` and
`set_default_logger_severity` are both present in 1.31.0 and are called at startup (§9). The
providers reported by this build are `AzureExecutionProvider` and `CPUExecutionProvider`; passing
`providers=["CPUExecutionProvider"]` explicitly keeps Azure out.

**`phonemizer` is noisy.** It logs `words count mismatch` warnings on most inputs; the
`phonemizer` logger is raised to `ERROR` so the log stays readable.

---

## 3. Decisions (approved during brainstorming)

| # | Decision |
|---|---|
| D1 | Trigger: mouse-select text, then a global hotkey. Primary Selection is the text source. |
| D2 | Topology **A**: one Qt process holding the warm model, plus a one-file Unix-socket control channel for the `utter` CLI. No D-Bus daemon, no process split. |
| D3 | Text source: Primary Selection, falling back to the clipboard when Primary is empty/stale. |
| D4 | Hotkeys: `org.freedesktop.portal.GlobalShortcuts` on Wayland, `python-xlib` `XGrabKey` on X11, `none` elsewhere. No `/dev/input`, no root, no fake. |
| D5 | Packaging: uv-managed venv + `.desktop` launcher. Not AppImage. |
| D6 | Model: `kokoro-v1.0.int8.onnx` + `voices-v1.0.bin`, SHA-256 pinned (§2.2), fetched by `install.sh` only. |
| D7 | G2P: espeak-ng phonemisation through `kokoro_onnx.tokenizer.Tokenizer`, behind a `Phonemizer` seam, plus a pre-normaliser (`tts/normalize.py`) for the input classes espeak-ng demonstrably misreads (§7.6). `misaki[en]` was measured and rejected for v1: 1.2 GB of PyTorch and ≈640 MiB extra RSS to fix the same defects. |
| D8 | Build sequence: one plan, three phase-gated increments (engine → UI → hotkey/installer), each verified end-to-end before the next. |
| D9 | Inference goes through `kokoro-onnx` 0.6.1 (pinned) rather than a hand-rolled tokenizer or inference path. |
| D10 | Synthesis is `Kokoro.create()` per Utter-sized text unit in our own worker thread — **not** `create_stream` (see §7.3). |
| D11 | Overlay is kept, with the Wayland positioning caveat documented; the tray menu duplicates every control. |
| D12 | `enable_cpu_mem_arena = False`; the model is loaded once and never unloaded (measured, §2.3). |

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
    normalize.py          pre-normalisation for espeak-ng's blind spots (§7.6)
    chunk.py              text → synthesis units (sizing rules in §7.3)
    phonemes.py           Phonemizer protocol; EspeakPhonemizer (wraps the library Tokenizer)
    model.py              model paths, SHA-256 check, session options, single lazy session
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
            Player.stop()                        audible stop is immediate
            worker (single thread) loops:
              take pending                                 wait when idle
              units = chunk.split_units(normalize(text))
              per unit:
                if generation != mine: drop it, take new pending (or exit)
                audio = kokoro.create(unit, voice, speed, lang, trim=True)   float32, 24 kHz
                queue.put((audio, pause_for(split_reason)), timeout=0.1)  bounded, maxlen 8
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

- Files under `$XDG_DATA_HOME/utter/models/`, digests per §2.2.
- `tts/model.py` builds the session itself, once per process (D12):

```python
ort.disable_telemetry_events()
ort.set_default_logger_severity(3)
opts = ort.SessionOptions()
opts.intra_op_num_threads = 4              # measured; 8 is 13% faster, 16 is slower (§2.3)
opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
opts.enable_cpu_mem_arena = False          # measured: 566 MiB -> 247 MiB steady (§2.3)
session = ort.InferenceSession(model_path, sess_options=opts,
                               providers=["CPUExecutionProvider"])
kokoro = Kokoro.from_session(session, voices_path)
```

- `CPUExecutionProvider` is passed explicitly, so an accelerated `onnxruntime` distribution cannot
  silently pull in a GPU provider (source spec §5, NFR 1), and this build's
  `AzureExecutionProvider` is never initialised (§9).
- The session is created lazily on the first `speak` and then kept for the process lifetime. It is
  never recreated: five sessions in one process peaked at 1282 MiB (§2.3).
- Voices come from the `.npz` loaded by the library; `voice_names()` returns `kokoro.voices.files`
  (54 voices) for the tray menu and `utter voices`.
- Speech parameters are validated by the library: `0.5 <= speed <= 2.0` (Utter's 0.75–2.0 slider is
  inside it, and 0.4/2.5 raise `ValueError`, which the engine catches).
- `model.py` contains no download code. The installer fetches; the runtime verifies SHA-256 against
  the digests in §2.2 and refuses to load on mismatch.

### 7.2 Phonemisation seam (D7)

`tts/phonemes.py` defines:

```python
class Phonemizer(Protocol):
    def phonemize(self, text: str, lang: str) -> str: ...
```

`EspeakPhonemizer` delegates to `kokoro_onnx.tokenizer.Tokenizer.phonemize` (espeak-ng with
`preserve_punctuation=True, with_stress=True`, filtered to the 114-entry vocab). The engine takes
one of two paths:

- **Default (`speech.phonemizer = "espeak"`):** call
  `kokoro.create(unit, voice=voice, speed=speed, lang=lang, trim=True)`, letting the library
  phonemise internally.
- **Alternative (`speech.phonemizer = "misaki"`):** call
  `kokoro.create(phonemizer.phonemize(unit, lang), voice=voice, speed=speed, lang=lang,
  is_phonemes=True)`.

The two paths were measured to produce **bit-identical** audio when fed the same phonemes (§2.3), so
this is a substitution of the phoneme string and nothing else. `EspeakPhonemizer` is the reference
implementation of the protocol, backing the seam's unit tests and the espeak half of the §11
comparison clips. `misaki[en]` was measured and rejected for v1 (§2.3): it needs an install-time
`en_core_web_sm` fetch, brings 1.2 GB of PyTorch and ≈640 MiB of extra resident memory, and its one
real advantage — normalising currency and numbers — is obtained from §7.6 for about a megabyte.
`speech.phonemizer` therefore keeps a single shipped value, and the seam stays as the documented
extension point.

### 7.3 Synthesis units (D10)

Synthesis is `Kokoro.create()` per unit, from our worker thread. `create_stream` is not used: it
batches up to `MAX_PHONEME_LENGTH = 510` phonemes per chunk, and the first chunk is only available
after that much audio has been synthesised, which conflicts with "audio starts fast after the
hotkey". Measured unit costs are in §2.3.

Unit sizing rules, in `tts/chunk.py`:

1. Split the text on sentence terminators (`.`, `!`, `?`, `…`) and newlines.
2. Drop punctuation-only pieces: a run whose non-whitespace content is entirely punctuation
   synthesises as near-silence, measured in §2.3. Emoji are kept, because espeak-ng speaks their
   names. Do not gate on `str.isalnum()`, which is `False` for emoji.
3. The **first piece** is cut short — at the first clause mark (`,`, `;`, `:`) or word boundary at
   or before 60 characters — and left whole when it is already at most that long. Measured: a
   19-character first unit costs 0.28 s of synthesis and a 39-character one 0.45 s (§2.3), which is
   where the §1.4 budget comes from.
4. Remaining pieces are sentences, split at clause marks, then word boundaries, then by hard slice
   at **80 characters**, choosing a cut that does not strand a one-word remainder.
5. Fragments shorter than two words merge into the neighbouring unit, forward when a later piece
   exists, otherwise backward. A merge may push a unit slightly past 80 characters: the cap
   governs splitting, not merging, and the alternative is an isolated one-word utterance.
6. Steps 2 and 5 both run on the emitted pieces, because steps 3 and 4 can manufacture a new
   punctuation-only piece (a leading comma cut off by itself) or a one-word piece (a sentence tail
   reduced to one word). Gating only the pre-split units leaves both defects in the output.
7. A unit's split reason comes from its own last non-space character: a sentence terminator is
   `SENTENCE`, a clause mark is `CLAUSE`, anything else is `WRAP`. The engine uses it only to pick
   the inter-unit silence (0.25 s or 0.12 s).
8. Input above `speech.max_chars` is refused before any synthesis begins.

The 80-character cap in rule 4 exists to bound the one thing the design cannot interrupt.
`InferenceSession.run()` cannot be cancelled mid-call, so the unit in flight when the user stops or
presses the hotkey again is the worst case for both CPU waste and responsiveness. The cap follows
the measured curve in §2.3: 86 phonemes ≈ 0.94 s of synthesis, 157 phonemes ≈ 2.2 s, and a
phoneme-dense 224-phoneme sentence ≈ 5.7 s. An 80-character cap holds the worst case near 1 s and
sits in the RTF 0.20 region, which is also the most CPU-efficient part of the curve. Phase 1 records
the real value for realistic text.

**Responsiveness invariant:** `SynthService` keeps a single worker thread and a latest-wins
pending-request slot (depth 1). `speak()` replaces any pending request and increments the
generation, so a new hotkey press is honoured as soon as the in-flight unit returns — never after
the whole previous text. If Phase 1 measures worse than ~1 s, the knob is the 80-character cap in
rule 4, not the thread topology.

Utter inserts its own silence between units, sized by why the split happened:
`speech.pause_sentence` (default 0.25 s) at a sentence boundary, and `speech.pause_clause` (default
0.12 s) at a clause or hard-wrap split inside a sentence, so an 80-character cut does not land like
a full stop. The library's own `sentence_pause` / `clause_pause` parameters are not used: they are
inert for this export (§2.3).

### 7.4 Playback

- `sounddevice.OutputStream(samplerate=24000, channels=1, dtype='float32')`, push mode, verified on
  this machine at 42.7 ms of latency with blocking writes pacing correctly (§2.3).
- The player thread pulls `(audio, pause)` pairs from the bounded queue (maxlen 8) and writes
  frames. A full queue blocks the worker, giving natural backpressure.
- **Pause** stops the player thread writing frames while leaving the stream open; PortAudio
  underruns to silence. Synthesis continues until the queue fills, then blocks. **Resume**
  continues from the next unwritten frame with no re-synthesis.
- **Stop** silences audio immediately: increment the generation, clear the queue, flush the
  stream, hide the overlay. The worker's in-flight unit finishes in the background and its result
  is discarded, so the only cost is CPU — bounded by one unit per §7.3.
- **Speed** is the library's duration-scaling parameter (pitch preserved, measured in §2.3), not
  resampling, so it applies per unit. A slider change takes effect on release and applies from the
  next synthesised unit; when idle it applies immediately. The overlay tooltip states this.
- Audio-device loss (route change, suspend/resume) is caught, the stream is reopened once, and on
  repeated failure playback stops cleanly with a notification.

### 7.5 Voices and language

The voice pack is loaded with the session and kept (D12). The language phoneme set is derived from
the voice-id prefix (`a`=en-us, `b`=en-gb, `e`=es, `f`=fr, `h`=hi, `i`=it, `j`=ja, `p`=pt-br,
`z`=zh), overridable with `speech.lang`. Default voice: `af_heart` (present in the pack). Only
`en-us`/`en-gb` are verified by this spec.

### 7.6 Text pre-normalisation (D7)

espeak-ng reads plain prose correctly but misreads three classes of input, measured in §2.3.
`tts/normalize.py` rewrites exactly those and nothing else, before chunking:

| Rule | Input | Rewritten as |
|---|---|---|
| Currency symbol before an amount, phrased by Utter over `num2words` cardinals | `$5.99`, `£5`, `€5`, `US$5` | `five dollars and ninety-nine cents`, `five pounds`, `five euros`, `five dollars` |
| Digit-group commas removed so the number is read as one value | `3,000`, `1,500,000` | `3000`, `1500000` |
| Leading-dot decimals given an explicit zero | `.5` | `0.5` |

Deliberately untouched, because espeak-ng already gets them right (§2.3): plain decimals (`1.5`
→ "one point five"), percentages, ordinals (`21st`), signed integers, thousands written without
separators, abbreviations (`Dr.`, `Etc.`, `e.g.`), slashes (`A/B`), clock times, the cent sign,
and Roman numerals. Dates and slash-separated forms are left alone because the locale's day/month
order is unknowable from the text.

**Why not `num2words`' own currency mode:** measured, it reads a bare integer amount as cents
(`num2words(5, to="currency", currency="USD")` → "zero dollars, five cents"), has no `JPY`, and
phrases GBP as "pounds sterling". Utter therefore phrases currency itself and uses `num2words`
only for the cardinal number, which is predictable. `normalize()` is disabled by
`speech.normalize = false`.

**Verified behaviour** (probe 9, throwaway): all 13 defect forms now read as intended, and 22
already-correct forms produce byte-identical phonemes after normalisation — a normaliser that
rewrote working text would be a regression, so those 22 forms are the permanent test battery in
Phase 1, alongside the 13.

Run order matters: normalise the whole selection first, then chunk it, so an amount is never
split across two synthesis units.

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
log_level = "info"

[speech]
voice = "af_heart"
speed = 1.0
lang = "en-us"
phonemizer = "espeak"      # the only shipped value; the seam allows others (see §7.2)
normalize = true           # rewrite espeak-ng's blind spots before synthesis (see §7.6)
max_chars = 20000
pause_sentence = 0.25      # silence after a sentence-boundary split, seconds
pause_clause = 0.12        # silence after a clause or hard-wrap split, seconds

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
`UTTER_*` environment variables < CLI flags. There is deliberately no `model_idle_unload_minutes`
and no pause-length keys: §2.3 measured both to be either useless or inert.

### 8.3 CLI

| Command | Behaviour |
|---|---|
| `utter` | Start the tray app. If a live socket already owns `utter.sock`, delegate "show" and exit 0. |
| `utter speak [TEXT]` | Delegate to the running instance; with no instance, run a cold in-process engine (logged as a cold start). `--text` and `--file` also accepted. |
| `utter stop` / `pause` / `resume` / `status` | Control socket. `status` prints JSON: speaking, paused, current unit, voice, speed, generation. |
| `utter voices [--json]` | List the 54 voices from the pack, marking the active one. |
| `utter doctor` | Resolved config, model paths and hashes, hotkey backend and portal app-id, audio device, last latency samples; non-zero exit when something is actually wrong. |
| `utter hotkey --show` | Re-open the portal binding dialog, or print bind instructions when no portal is available. |

The socket protocol is JSON-lines: one request object per line, one response object per line, plus
`{"event": …}` lines for state changes. A stale socket file is detected by connect-probe and
unlinked before rebinding.

---

## 9. Error handling and the air-gap guarantee

- Worker exceptions are caught per unit: log the traceback, notify `Speech failed: <reason>`, keep
  the app alive, and restart the worker on the next `speak`. `ValueError` from the library means
  "nothing to synthesise" for empty or whitespace-only input (§2.3) and is reported as such.
- Missing or hash-mismatched model → notification plus a `doctor` diagnosis; never a download.
- Empty selection → one tray message, no modal, no audio state change.
- Audio failure → one reopen attempt, then a clean stop with a notification.
- Declined hotkey binding → notification plus CLI-bind instructions; tray and CLI keep working.
- Single-instance conflicts (stale socket, foreign owner of the path) are resolved by
  connect-probe, not PID files.
- Logging: `ort.disable_telemetry_events()` and `ort.set_default_logger_severity(3)` at startup;
  the `phonemizer` logger is raised to `ERROR` because it logs `words count mismatch` on most
  inputs (§2.3). One line per utterance records unit count, audio duration, synthesis time and
  time-to-first-frame — the source of the numbers in §11. No usage data leaves the machine.

**Air gap, structurally enforced:** all download code lives in `install.sh`; `src/utter` contains
no HTTP client. Phase-3 verification greps `src/utter` for
`urllib|requests|httpx|socket.create_connection` (only AF_UNIX uses may match) and runs the app
under `strace -f -e trace=network` while speaking, confirming zero non-unix socket syscalls. This
check also covers whichever phonemizer is configured, because a G2P that fetches models at runtime
would break the air gap; §11 verifies `misaki` offline before it can be adopted.

---

## 10. Installer and packaging

`install.sh`, bash, `set -euo pipefail`. Flags: `-y|--yes`, `--no-autostart`, `--dry-run`,
`--uninstall [--purge]`, `--prefix DIR` (redirects all XDG roots for testing).

1. **Preflight report** — session type, desktop, compositor, audio server; presence of `uv` and
   `wl-clipboard`; prints the exact `sudo dnf install …` line for anything missing. `espeak-ng` is
   reported as optional, since `espeakng-loader` bundles the library and data. The script never
   invokes `sudo` itself.
2. **Consent gate** — prints the download ledger (model 88.07 MiB + voices 26.91 MiB + CPython 3.12
   ≈30 MiB + wheels ≈250 MiB) and requires `y`; the default answer is `n`. `--yes` skips the
   prompt. This is source spec §Build.1.
3. **Install** — `uv venv --python 3.12 $XDG_DATA_HOME/utter/venv`, then install this package into
   it with `kokoro-onnx==0.6.1` pinned (via the project's `pyproject.toml`).
4. **Fetch** — exactly two files, with digests pinned in the script as literals:

   | File | URL (release `model-files-v1.0` of `thewh1teagle/kokoro-onnx`) | SHA-256 |
   |---|---|---|
   | `kokoro-v1.0.int8.onnx` | `…/download/model-files-v1.0/kokoro-v1.0.int8.onnx` | `6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb` |
   | `voices-v1.0.bin` | `…/download/model-files-v1.0/voices-v1.0.bin` | `bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d` |

   Use `curl -fL -C -` into a temp directory, `sha256sum -c` the result, then move atomically into
   `models/`; on mismatch delete and abort printing expected versus actual. Digests are recorded in
   `config.toml`.
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

**Settled before planning** (probe artifacts under `/tmp/utterprobe`, throwaway): thread/execution
mode choice, arena setting, idle-unload viability, real-time factor, unit costs, pause-parameter
behaviour, speed range, G2P seam equality, degenerate-input behaviour. Results are in §2.3.

**Phase 1 — engine**
- Unit: unit sizing (first-unit clause cut, sentence split, >80-char split, small-fragment merge,
  punctuation-only skip, `max_chars` refusal); normalisation (the 13 defect forms of §7.6 fixed and
  the 22 correct forms byte-identical); capture fallback matrix with injected runners; config
  round-trip and precedence; engine generation-cancellation with a fake synthesizer; latest-wins
  pending request superseding an in-flight text; player state machine (stop/pause/resume/drain)
  with a fake stream; `ValueError` from the library mapped to a user-facing message.
- Real smoke, on CPU: `utter speak --text <paragraph>`. Record model load time, per-unit synthesis
  time, real-time factor, **time-to-first-frame**, **worst-case responsiveness** (time from a
  superseding `speak()` to the first frame of the new text — the §7.3 invariant), and peak RSS
  (budget ≤300 MiB). Write a WAV and verify sample rate, duration and non-silence out-of-band.
- Deliverable: the espeak-vs-misaki comparison clips produced before planning
  (`/tmp/utterprobe/speak-espeak.wav`, `/tmp/utterprobe/speak-misaki.wav`) were used for the D7
  decision, which rejected misaki on measured cost; keep them as recording of why. Re-run the
  clips after §7.6 lands so the audible comparison reflects the shipped normaliser.

**Phase 2 — UI**
- Launch the real app on the KDE Wayland session; confirm the StatusNotifierItem registers
  (`busctl --user`); screenshot the overlay; drive pause → resume → stop mid-sentence while
  observing queue and stream transitions.
- Measure whole-app RSS with Qt loaded against the ≤300 MiB criterion (§1.3).
- Run `utter speak` from a terminal while the app runs, proving socket delegation and no second
  tray instance.

**Phase 3 — hotkey and installer**
- `install.sh --dry-run`, then a real install into a temporary XDG root, then a real install.
- Trigger the portal `BindShortcuts` dialog; confirm `Activated` fires and audio plays for
  mouse-selected text in Kate and Firefox.
- Exercise the X11 path under `Xephyr`. **Prerequisite: Xephyr is not installed on this machine**
  (nor Xvfb or Xorg), so Phase 3 must `sudo dnf install xorg-x11-server-Xephyr` or the X11 backend
  ships with only its unit tests and an explicit "not exercised on X11" note.
- Confirm zero non-unix network syscalls under `strace` while speaking, with the configured
  phonemizer.
- Verify the pinned SHA-256 digests reject a corrupted download.
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
| 1 | `tts/` engine + `capture.py` + config + `utter speak` CLI | A paragraph is synthesised on CPU from the CLI; latency and RSS measured against §1; comparison clips judged |
| 2 | Tray icon, overlay, control socket, full CLI | Pause/resume/stop observed on real audio; tray visible on Wayland; CLI delegates to the warm process; RSS ≤300 MiB |
| 3 | Hotkeys, installer, desktop integration, autostart | Hotkey speaks a mouse selection in a real app; digests verified; `--uninstall` leaves no residue |

Each phase must run end-to-end before the next begins.

---

## 13. Risks

| Risk | Mitigation |
|---|---|
| **Steady-state RSS is ~247 MiB, above the source spec's 120–200 MB** | Measured floor: 191 MiB for the session alone, 247 MiB steady. Recorded as a deviation in §1.3 with the arena disabled; the remaining lever is unit size, already capped |
| **First sound is ~0.4 s, not the source spec's 150 ms** | Measured in §2.3. 150 ms only buys ~12 phonemes; a clause-sized first unit is the honest trade. A ~20-character first unit would approach 0.2 s at a prosody cost — a listening decision in Phase 1 |
| `misaki[en]` was rejected, so if it is ever adopted its install-time `en_core_web_sm` fetch and its `SystemExit` behaviour must be handled | Measured (§2.3). Adoption means the installer provisions the model, the engine catches `SystemExit` as well as `Exception`, and the download ledger grows from ≈400 MB to ≈1.7 GB |
| The normaliser rewrites text that espeak-ng already read correctly | Rules are limited to the three measured classes, and the 22-form regression battery of §7.6 asserts byte-identical phonemes for everything else |
| espeak-ng misreads currency, digit-group commas and leading-dot decimals | Measured (§2.3) and fixed by `tts/normalize.py` (§7.6); verified 13 of 13 forms fixed with no regressions |
| First-unit clause cut sounds discontinuous against the next unit | `pause_clause` between units; if audible, rule 3 falls back to first-unit = first sentence — decided by listening in Phase 1 |
| Worst-case responsiveness grows because RTF rises with unit length (§2.3) | Unit cap reduced to 80 characters on the measured curve; the cap, not the thread topology, is the knob |
| CPU memory arena growth on long texts | Arena disabled (D12); measured steady at 247 MiB across repeat rounds |
| Pinning `kokoro-onnx==0.6.1` blocks a future Python upgrade | It is the only Python-version constraint (`<3.14`); upgrade path is the library's own release cadence |
| Portal app-id mismatch when launched from a bare terminal | Pre-Qt `Register`, launch from `.desktop`/autostart, and `doctor` reporting the app-id in use |
| `XGrabKey` conflicts with existing DE shortcuts | Configurable triggers; grab failure logged and reported by `doctor`; the CLI path is always available |
| Wayland overlay lands wherever the compositor puts it | Tray menu carries identical controls; limitation documented in the README and the overlay tooltip |
| int8 quality loss versus fp16 | Both files are pinned by URL in the installer; the fp16 swap is a flag plus a code change to §7.1's file list |

## 14. Licensing and attribution

Utter's own code is Apache-2.0. Kokoro-82M and its ONNX export are Apache-2.0; `kokoro-onnx` is
MIT; espeak-ng is GPL-3.0 and is used as a bundled shared library by `espeakng-loader` (no linking
by Utter's code). `num2words` (LGPL-2.1, 1.3 MB) and `docopt` (MIT) are installed as separate
libraries for §7.6; the LGPL obligation is met by them remaining replaceable installed packages,
and no LGPL code is copied into Utter. The README credits `hexgrad/Kokoro-82M` (model),
`thewh1teagle/kokoro-onnx` (ONNX export, voice pack, Python bindings), `espeak-ng` (G2P) and
`num2words` (number verbalisation).
