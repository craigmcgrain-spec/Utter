# Utter

Utter speaks the text you select. Select a sentence in any application, press the hotkey, and
hear it read aloud by a local quantised [Kokoro 82M](#credits-and-licence) model.

Everything runs on this machine. `src/utter` contains no HTTP client at all — `install.sh` is
the only file that ever downloads anything, and only after you say yes — and the app executes on
the CPU with no GPU, CUDA or Vulkan dependency. One tray process stays resident with the model
loaded, so the second selection starts speaking in about half a second.

The tray icon carries Speak selection, Pause/Resume, Stop, the 54-voice list, speed, Start at
login and Quit; a small floating controller over the desktop carries pause, resume, stop and the
speed slider while speech plays. The voice and speed you pick are written to `config.toml` and
survive a restart.

## Requirements

- **Linux** with a desktop session. KDE Plasma on Wayland is what the project was built and
  measured on; GNOME, other compositors and X11 are supported (see [Hotkeys](#hotkeys)).
- **PipeWire or PulseAudio** — playback goes through PortAudio, so the usual `libportaudio` and
  `libpulse` stack is needed.
- **`uv`** — the installer creates the virtualenv with it; you are never asked to make one.
- **≈400 MiB of disk** for the two model files, CPython 3.12 and the wheels. The installer prints
  the exact ledger and asks before downloading anything.

On Fedora the audio stack is already there; the installer's preflight report prints the exact
`sudo dnf install …` line for anything missing and never runs `sudo` itself.

## Install

From a checkout of this repository:

```bash
bash install.sh
```

`install.sh` reports your session, desktop, compositor and audio server, then asks before
downloading. Useful flags:

| Flag | What it does |
|---|---|
| `-y`, `--yes` | Skip the consent prompt (the default answer is `n`). |
| `--no-autostart` | Do not install an autostart entry (otherwise the script asks). |
| `--dry-run` | Print what would be fetched and installed, and change nothing. |
| `--prefix DIR` | Put every XDG root under `DIR` instead of `$HOME`. For testing. |
| `--uninstall [--purge]` | See [Uninstall](#uninstall). |

It installs the app into `~/.local/share/utter/venv`, the two model files into
`~/.local/share/utter/models/`, a launcher at `~/.local/bin/utter`, a desktop entry and icon,
and — if you asked for it — an autostart entry. Re-running it is idempotent.

### What it downloads, and why the digests matter

| File | Size | SHA-256 |
|---|---|---|
| `kokoro-v1.0.int8.onnx` | 92 361 271 B (88.07 MiB) | `6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb` |
| `voices-v1.0.bin` (54 voices) | 28 214 398 B (26.91 MiB) | `bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d` |

Both come from the `model-files-v1.0` release of `thewh1teagle/kokoro-onnx`. The installer
downloads them with `curl -fL -C -` into a temporary directory, checks them with `sha256sum -c`
and only then moves them into place; a mismatch deletes the file and aborts with the expected and
actual digests, and a file already in `models/` whose digest matches is never fetched again. The
verified digests are recorded alongside the install records in `config.toml`, and the app checks
the pack against them before it loads it — `utter doctor` prints the digest recorded for each file
and the result of checking it, including the case where none was recorded.

**The URL-and-digest pairing rule.** `UTTER_MODEL_URL` and `UTTER_VOICES_URL` override where each
file is fetched from. A digest override — `UTTER_MODEL_SHA256`, `UTTER_VOICES_SHA256` — is
honoured **only when the matching URL override is set too**. The pairing is the point: a stray
`UTTER_MODEL_SHA256` in a shell cannot weaken a download that still points at the pinned release,
and the installer's own tests can serve a local file and declare its digest without fetching
115 MiB.

## Use

```bash
utter                      # start the tray (or ask a running instance to show itself)
utter speak                # speak the current selection
utter speak "hello there"  # speak this text
utter speak --file notes.md
utter speak --text "…" --save-wav /tmp/out.wav   # write 24 kHz mono WAV, play nothing
utter stop | pause | resume
utter status               # JSON: generation, state, voice, speed, paused
utter voices [--json]      # the 54 voices in the pack, the active one marked
utter doctor               # explain this machine's setup (see below)
utter hotkey --show        # re-open the shortcut dialog, or print the steps to bind
```

`utter speak` hands the text to the running instance; with no tray running it builds an engine in
its own process (logged as a cold start) and waits for its own audio to finish before exiting.
`utter stop`, `pause`, `resume`, `status` and a bare `utter hotkey` are meaningful only against a
running instance: with nothing listening they say so and exit non-zero rather than inventing an
answer. `utter doctor` and `utter hotkey --show` are the opposite: they always produce an answer
rather than an error, which is what makes them usable exactly when something is broken.

### `utter doctor`

One page about the setup: the resolved configuration (the file plus any `UTTER_*` overrides), the
config path and whether it could be read, each model file with the digest recorded at install time
and the result of checking it, the hotkey backend your session selects together with the portal
app-id it registers, the default output device, and the last five time-to-first-audio samples from
the log. It never raises and never needs a running instance — a missing model, no audio device, no
session bus and an unreadable `config.toml` are all lines in the report — and it exits non-zero
only when the app could not work as configured (a pack file missing or failing its digest, no
output device, or a config it cannot read).

```text
$ utter doctor                       # the real output, with paths and digests shortened
instance:        not running
config:          /home/you/.config/utter/config.toml
general:         log_level=info autostart=False
speech:          voice=af_heart speed=1.0 lang=en-us phonemizer=espeak normalize=True max_chars=20000 pause_sentence=0.25 pause_clause=0.12
hotkey:          speak=LOGO+ALT+s stop=LOGO+ALT+x pause=LOGO+ALT+p
ui:              show_overlay=True overlay_pos=(0, 0)
install:         consent_given=True
hotkey backend:  portal (configured auto, session wayland, portal reachable, portal app-id utter)
output device:   default (128 channels, 44100 Hz)
models:          /home/you/.local/share/utter/models
  /home/you/.local/share/utter/models/kokoro-v1.0.int8.onnx  sha256=6e7421…  sha256 ok
  /home/you/.local/share/utter/models/voices-v1.0.bin  sha256=bca610…  sha256 ok
pack check:      ok
paths:           log=/home/you/.local/state/utter/utter.log socket=/run/user/1000/utter.sock
latency:         1.214 0.478 0.500 0.484 (seconds to first audio, oldest first, last 4 utterances; see …/utter.log)
```

Failures read the same way, with the reason in place of the state — `config: … (unreadable: …;
the built-in defaults are in use)`, `missing`, `pack check: model file not found: …`, `output
device: unavailable: …` — and any of them makes the command exit 1.

## Hotkeys

The defaults are `Super+Alt+S` (speak the selection), `Super+Alt+X` (stop) and `Super+Alt+P`
(pause/resume). They are configurable in the `[hotkey]` section of `config.toml`.

- **Wayland with `xdg-desktop-portal` (KDE, and GNOME with the portal implementation):** Utter
  uses the desktop's own `GlobalShortcuts` portal, so the desktop shows a one-time configuration
  dialog in which you confirm or change the three shortcuts. The portal requires the app-id to
  match an installed `.desktop` file's basename, which is why the installer writes
  `~/.local/share/applications/utter.desktop` — a bare `python -m utter` from a terminal cannot
  register itself and will fall back to the manual instructions below.
- **X11:** Utter grabs the keys itself with `XGrabKey`. `[hotkey] backend = "x11"` forces this
  path; `backend = "x11"` is also what `auto` picks on an X11 session. The verification phases
  reported in `docs/` ran on Wayland sessions, so this path's evidence is its unit tests.
- **GNOME on Wayland without the portal implementation:** no global hotkey is possible without a
  shell extension, and Utter does not pretend otherwise. It notifies once and keeps working
  through the tray — so bind the CLI to a shortcut yourself:
  **Settings → Keyboard → Custom Shortcuts → `+` → Command `utter speak`**, then press the
  combination you want. `utter hotkey --show` prints these same steps whenever there is
  nothing that can open a dialog (no instance running, or no portal behind it), and asks a
  running instance to re-open its dialog when there is one. (The equivalent fallback
  *is* exercised: the project's machine took it before its desktop entry existed, because the
  portal refuses an app-id with no `.desktop` file behind it — see the phase 2 report. The
  GNOME-without-portal case itself was not measured.)

If a bind is refused or a key combination is already taken, Utter notifies you once with the same
instructions and keeps running; nothing else about the app depends on a hotkey.

## Known limitations

- **The overlay lands where the compositor puts it on Wayland.** Wayland forbids client-side
  positioning of top-level windows, so `[ui] overlay_pos` is honoured on X11 only and the widget
  appears wherever the compositor decides. Every control the overlay carries is duplicated in the
  tray menu, so nothing is reachable only through it.
- **Only `en-us` and `en-gb` are validated.** The other languages in the pack work through
  espeak-ng's G2P, but the project's verification plan only covers English.
- **No word-level highlighting or scrubbing.** The pinned ONNX export has no duration output, so
  phoneme timings are unavailable.
- **No AT-SPI reading of the focused widget**: Utter speaks the primary selection (or the
  clipboard), which is what a mouse selection leaves behind.

## Measured behaviour, and where it misses the targets

The numbers below are copied from the phase reports in `docs/` (`phase1-report.md` measures the
one-shot CLI, `phase2-report.md` the assembled tray app). They are not estimates, and the two
series are not combined.

| | Measured | Where it came from |
|---|---|---|
| Model load | 0.48–0.53 s | `model load=` log line, 19 runs (Phase 1) |
| Cold time to first audio | 1.00–1.11 s, of which ≈0.49 s is the model load | `ttfirst=` log line (Phase 1) |
| Warm time to first audio | 0.52–0.64 s, typically ≈0.55 s | Phase 1, derived from the two lines above |
| Tray: first utterance | 1.214 s | `ttfirst=` (Phase 2) |
| Tray: repeated paragraph | 0.478, 0.500, 0.484 s | `ttfirst=` (Phase 2) |
| Tray: one-unit utterances | 0.611 s and 0.815 s | `ttfirst=` (Phase 2) |
| Synthesis speed | real-time factor 0.25–0.28 (25.56 s of audio in 6.47–7.16 s) | `utterance` lines (Phase 1) |
| Tray idle, model resident | 275.8 MiB, flat across five samples | `/proc/<pid>/status` `VmRSS` (Phase 2) |
| CLI steady state | 198–367 MiB over nine runs, median ≈240 MiB; one run above 320 | `VmRSS` (Phase 1) |
| Tray speaking | median 317.5 MiB over ten samples, five of them above 320 MiB | `VmRSS` (Phase 2) |
| Peak RSS (allocator-dominated) | 465–943 MiB session-wide; tray high-water ≥879.7 MiB | `/usr/bin/time -v` (Phase 1), `VmHWM` (Phase 2) |

**These are deviations from the source spec, recorded rather than hidden.** The source spec's
targets were 120–200 MB of steady-state memory and ≤150 ms to first sound. Neither is attainable with this
stack: the model plus the ONNX Runtime session floors at ≈182 MiB before any audio is produced,
and 150 ms buys about two words of speech, so the first unit is clause-sized instead. The criteria
were therefore restated as distributions with their tails reported — median ≤320 MiB steady state
and ≤0.6 s warm first sound — and read that way the CLI's steady state (median ≈240 MiB) and the
tray's idle and typical figures pass, while the tails above do not. The tray's first utterance
pays a one-time warm-up, and a one-unit utterance pays the same fixed per-utterance set-up with
almost no audio to spread it over, which is why both are slower than a repeated paragraph.

**What has not been verified, and is not claimed:** nobody has listened to the audio. Playback is
evidenced by a live 24 kHz mono PipeWire stream and the engine's own timings, not by ear. The
tray menu items, the overlay's buttons, text-less selection capture and the portal's shortcut
dialog were not driven on the project's machine; the phase 2 report lists each one.

## Configuration and files

| Purpose | Path |
|---|---|
| Config | `~/.config/utter/config.toml` |
| Log | `~/.local/state/utter/utter.log` (1 MiB × 3 rotating) |
| Models | `~/.local/share/utter/models/` |
| Venv | `~/.local/share/utter/venv/` |
| Control socket | `$XDG_RUNTIME_DIR/utter.sock` (mode 0600) |
| Launcher | `~/.local/share/applications/utter.desktop` |
| Autostart | `~/.config/autostart/utter.desktop` |

```toml
[general]
autostart = false
log_level = "info"

[speech]
voice = "af_heart"          # any of the 54 ids from `utter voices`
speed = 1.0                 # clamped to 0.75–2.0
lang = "en-us"
phonemizer = "espeak"       # the only shipped value
normalize = true            # rewrite the currency/number forms espeak-ng misreads
max_chars = 20000
pause_sentence = 0.25       # silence after a sentence split, seconds
pause_clause = 0.12         # silence after a clause or hard wrap

[hotkey]
backend = "auto"            # auto | portal | x11 | none
speak = "LOGO+ALT+s"
stop = "LOGO+ALT+x"
pause = "LOGO+ALT+p"

[ui]
show_overlay = true
overlay_pos = [0, 0]        # honoured on X11 only

[install]
consent_given = false
model_sha256 = ""           # recorded by the installer; the app checks the pack against it
voices_sha256 = ""
```

Precedence is built-in defaults < `config.toml` < environment, where an environment override is
named `UTTER_<SECTION>_<KEY>` (for example `UTTER_SPEECH_VOICE=af_bella`). Editing the file by
hand is fine; Utter rewrites it atomically when the tray persists a voice or speed.

## Uninstall

```bash
bash install.sh --uninstall            # stops a running instance, removes everything but your config
bash install.sh --uninstall --purge    # removes ~/.config/utter/config.toml too
```

The plain form stops a running instance and removes the venv, the model files, the log directory,
the desktop entry, the icon, the autostart entry and the `~/.local/bin/utter` shim. It leaves
`config.toml` where it is, so a later reinstall keeps your settings.

## Air gap

Utter is built for a machine that stays off the network. There is no download code in
`src/utter` — a test scans the package for HTTP clients and for sockets that are not `AF_UNIX`,
so a client cannot be added by accident — and the project's verification procedure traces the
running app with `strace -f -e trace=network` while it speaks, expecting no `AF_INET`/`AF_INET6`
socket at all (D-Bus and the control socket are unix sockets and expected). Only the
consent-gated installer touches the network, and only for the two files and the packages listed
above.

## Credits and licence

Utter's own code is **Apache-2.0** (see `LICENSE`). It stands on other people's work:

- [`hexgrad/Kokoro-82M`](https://huggingface.co/hexgrad/Kokoro-82M) — the model (Apache-2.0).
- [`thewh1teagle/kokoro-onnx`](https://github.com/thewh1teagle/kokoro-onnx) — the ONNX export,
  the voice pack and the Python bindings (MIT).
- [`espeak-ng`](https://github.com/espeak-ng/espeak-ng) — the grapheme-to-phoneme step, used as a
  bundled shared library (GPL-3.0; `espeakng-loader` packages it, and no espeak code is linked
  into or copied into Utter).
- [`num2words`](https://github.com/savoirfairelinux/num2words) — number verbalisation
  (LGPL-2.1, installed as a separate replaceable package).
