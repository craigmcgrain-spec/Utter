# Utter — Phase 2 verification report

The assembled app, not a CLI: the tray process from `uv run utter` on this machine (KDE Plasma 6
on Wayland, PipeWire, dual 3840×1080 desktop). One live instance, PID **76735**, started at
06:48:36 and driven for the whole session. Everything below was taken from that instance; every
number names the command or file it came from.

Phase 1 measured the one-shot CLI process. **Nothing here is combined with Phase 1's numbers**:
the RSS series below are the tray's own, sampled from `/proc/76735/status`.

## Step 1 — launch and tray registration

```text
$ uv run utter &          # stdout/stderr → /tmp/utter-phase2/app.log
$ pgrep -af "bin/utter"
76735 …/.venv/bin/python …/.venv/bin/utter
$ pgrep -fc "bin/utter"
1
```

Exactly one process. It stays one for the whole session (checked again in Step 3).

Startup printed one non-fatal warning (the portal app-id the shortcuts API requires is not
installed on this machine, so the global hotkeys are unbound and §6.3's manual instructions are
the path taken; the app keeps running):

```text
could not register the app-id 'utter' with the portal; global shortcuts may be unavailable
  … utter.hotkey.PortalError: org.freedesktop.portal.Error.Failed: ("Could not register app ID: App info not found for 'utter'",)
$ grep -E "not bound" ~/.local/state/utter/utter.log | tail -1
2026-10-11 06:48:36,511 WARNING utter.hotkey: the global hotkeys are not bound (org.freedesktop.portal.Error.NotAllowed: ('An app id is required',)); no global hotkey is active; either grant the portal dialog or bind 'utter speak' yourself under Settings -> Keyboard -> Custom Shortcuts
```

**The brief's Step-1 probe does not name this app's bus name.** KDE/Qt6 registered the item on a
*unique* connection name, not `org.kde.StatusNotifierItem-<pid>-1`, so the literal command fails:

```text
$ busctl --user tree org.kde.StatusNotifierItem-76735-1
Failed to introspect object / of service org.kde.StatusNotifierItem-76735-1: The name is not activatable
No objects discovered.
```

The item *is* registered. `busctl --user list` shows the pid's connections, and the watcher lists
one of them:

```text
$ busctl --user list | grep 76735
:1.824  76735  utter  mcgrain  …        # (+ :1.829 :1.830 :1.831 :1.832)
$ gdbus call --session --dest org.kde.StatusNotifierWatcher --object-path /StatusNotifierWatcher \
    --method org.freedesktop.DBus.Properties.Get org.kde.StatusNotifierWatcher RegisteredStatusNotifierItems
(<[':1.84/StatusNotifierItem', ':1.222/org/ayatana/NotificationItem/parakeet_cage', ':1.254/StatusNotifierItem', ':1.832/StatusNotifierItem']>,)
```

`:1.832` is owned by PID 76735, and introspecting `:1.832 /StatusNotifierItem` identifies it as
utter's:

```text
.Id        "utter"
.Title     "utter"
.Category  "ApplicationStatus"
.Status    "Active"
.IconName  "audio-volume-medium"
.Menu      "/MenuBar"
```

**Result: PASS.** `utter` is among the registered `RegisteredStatusNotifierItems` (as
`:1.832/StatusNotifierItem`), and exactly one process serves it.

## Step 2 — playback controls on real audio, and the overlay

Driven through the control socket (`utter speak/pause/resume/stop/status/voices`), because the
tray menu itself is pointer-driven and could not be used (see *What could not be driven*).

The sample paragraph (`docs/sample-paragraph.txt`, the same text Phase 1 used) was spoken by the
**running instance** — the CLI delegated (it returned at once, generation advanced, and no
`cold start` line was written).

```text
$ uv run utter status                          # idle, before anything
{"ok": true, "generation": 0, "state": "IDLE", "voice": "af_heart", "speed": 1.0, "paused": false}
$ uv run utter speak --text "$(cat docs/sample-paragraph.txt)"; echo $?
0
$ uv run utter status
{"ok": true, "generation": 1, "state": "PLAYING", "voice": "af_heart", "speed": 1.0, "paused": false}
$ uv run utter pause
$ uv run utter status
{"ok": true, "generation": 1, "state": "PAUSED", "voice": "af_heart", "speed": 1.0, "paused": true}
$ uv run utter status                          # still paused 4 s later
{"ok": true, "generation": 1, "state": "PAUSED", "voice": "af_heart", "speed": 1.0, "paused": true}
$ uv run utter resume
$ uv run utter status
{"ok": true, "generation": 1, "state": "PLAYING", "voice": "af_heart", "speed": 1.0, "paused": false}
$ uv run utter status                          # still playing 2 s later
{"ok": true, "generation": 1, "state": "PLAYING", "voice": "af_heart", "speed": 1.0, "paused": false}
$ uv run utter stop
$ uv run utter status
{"ok": true, "generation": 2, "state": "IDLE", "voice": "af_heart", "speed": 1.0, "paused": false}
```

**Resume continues rather than restarts.** Pause and resume left `generation` at **1** — the
generation only advances when a new utterance is accepted — and the engine log has exactly **one**
`utterance` line for that whole speak/pause/resume cycle:

```text
$ grep "utterance " ~/.local/state/utter/utter.log | awk '$2 >= "06:48"'
2026-10-11 06:49:04,126 INFO utter.tts.engine: utterance units=9 audio=25.56 synth=7.25 ttfirst=1.214
…
```

A resume that restarted would have called `speak()` again: a new generation (2) and a second
`utterance` line at that moment. Neither happened. `stop` did advance the generation (1 → 2),
which is what invalidates the in-flight utterance.

### Screenshot while speaking

Taken with `spectacle` mid-utterance (state `PLAYING`, generation 6):

- Full desktop capture: **`/tmp/utter-phase2/speaking2.png`** (3840×1080; the first, equivalent
  capture is `/tmp/utter-phase2/utter-speaking.png`).
- Crop of the floating controller, committed with this report: **`docs/phase2-overlay.png`**.

I looked at both files. `docs/phase2-overlay.png` shows the overlay while the paragraph plays:
the **Pause** and **Stop** buttons, the speed slider and the voice label **af_heart**. That is a
visual check I performed; I did not click any of those controls (see below).

### Voices answered by the live instance

```text
$ uv run utter voices --json
{"voices": ["af_alloy", … "zm_yunyang"], "active": "af_heart"}
```

54 voices, active `af_heart`. Served by the tray, not a cold engine: there are **zero** `cold
start` log lines after 06:48 (the 19 in the file are all Phase 1).

### Audio really reached an output device

`pactl` shows a live playback stream with the player's own frame rate while utter spoke — this is
the tray's stream (the only Python ALSA playback node on the machine, at 24 kHz):

```text
$ pactl list sink-inputs | grep -A12 'PipeWire ALSA \[python3.12\]'
Sink Input #1974
  Corked: no
  application.name = "PipeWire ALSA [python3.12]"
  node.name = "alsa_playback.python3.12"
  stream.is-live = "true"
  node.rate = "1/24000"
  node.latency = "208/24000"
```

**I did not listen to it.** The stream's existence, liveness and 24 kHz rate are what I measured;
"the speech sounds correct" is not a claim in this report.

## Step 3 — CLI delegation and whole-app RSS

Delegation: every `utter` command in this session returned at once and was answered by PID 76735
(`cold start` count after 06:48: **0**), while `speak` still advanced the tray's generation and
produced a real `utterance` line. `utter stop`/`pause`/`resume`/`status` are live-only by design
and all succeeded, which is itself proof the socket was served.

Process count, re-checked while playing: `pgrep -fc "bin/utter"` → **1**.

RSS is `VmRSS` from `/proc/76735/status`, five samples one second apart, recorded as their **own**
series (these are the tray's numbers, not Phase 1's CLI numbers).

**Idle** (model resident, nothing spoken), taken before the first utterance of the session:

| sample | VmRSS (MiB) |
|---|---|
| idle 1 | 275.789 |
| idle 2 | 275.789 |
| idle 3 | 275.789 |
| idle 4 | 275.789 |
| idle 5 | 275.789 |

median **275.789 MiB** (identical every sample).

**While speaking**, run A — samples 1 s apart beginning ≈4 s after `speak`:

| sample | VmRSS (MiB) |
|---|---|
| A1 | 346.555 |
| A2 | 540.566 |
| A3 | 275.816 |
| A4 | 275.816 |
| A5 | 275.816 |

median **275.816 MiB**; max **540.566 MiB**.

**While speaking**, run B — the same protocol, samples beginning immediately after `speak` (so
they land on the synthesis-heavy window rather than only the drain):

| sample | VmRSS (MiB) |
|---|---|
| B1 | 283.770 |
| B2 | 346.551 |
| B3 | 376.734 |
| B4 | 288.375 |
| B5 | 346.566 |

median **346.551 MiB**; max **376.734 MiB**.

Process high-water mark (extra context, not one of the two series): `VmHWM` read as **879.66 MiB**
after run B. The process went on to speak twice more before it was stopped, so that is a
floor on the final high-water mark, not the final value.

### Verdict against §1.3

The spec's restated criterion is **median ≤ 320 MiB, with the tail reported** (steady-state RSS in
the typical case; the design's 247 MiB is the trimmed floor).

| Population | Samples | Median | Verdict |
|---|---|---|---|
| Idle / steady state (model resident) | 5 | **275.789 MiB** | **PASS** |
| Speaking run A | 5 | 275.816 MiB | PASS |
| Speaking run B | 5 | 346.551 MiB | **above 320** |
| All speaking samples | 10 | **317.463 MiB** | PASS, marginally |
| All samples (idle + speaking) | 15 | 275.816 MiB | PASS |

**PASS against the restated criterion**, read both as the steady state (275.8 MiB, dead flat) and
as every speaking sample taken together (median 317.5 MiB). **Tail, reported**: 2 of the 10
speaking samples exceed 320 MiB (346.6 and 540.6 MiB) and run B's five-sample median alone is
346.6 MiB; the process high-water mark is ≥ 879.7 MiB. The excess sits in the synthesis window —
the two run-A samples taken after synthesis finished had already returned to 275.8 MiB while the
audio was still playing, i.e. the tray trims back to its 275.8 MiB steady state once the
transient passes. Against the **original** §1.3 wording (`≤ 300 MiB`) the idle/steady state still
**passes** at 275.8 MiB; the speaking tail is the part that exceeds it.

Phase 1's settled CLI figures were 198–367 MiB (median ≈240). The tray's steady state is higher
by roughly Qt's footprint (275.8 vs ~240) and lower than Phase 1's *peak* range (465–943 MiB) —
this is the first measurement of the whole app with Qt loaded, which Phase 1 explicitly deferred.

Warm time to first frame observed in the tray (context, §1.4's restated ≤ 0.6 s bound): the
paragraph's four utterances logged `ttfirst` **0.478, 0.484, 0.500, 0.500 s** (the tray loads the
model once at startup, `model load=0.58s`, `config` at 06:48:56). Not a Phase-2 acceptance
criterion, recorded because it was free.

## What could not be driven

Honest list of the checks a pointer (or I) could not perform. Nothing below is claimed as
verified anywhere above.

1. **The tray menu items** — *Speak selection*, *Pause/Resume*, *Stop*, the voice and speed
   submenus, *Start at login* and *Quit*. The KDE system-tray icon lives in the hidden-items popup
   and there is no way for me to click a tray menu on Wayland. The playback controls were driven
   through the control socket instead (Step 2).
2. **The system-tray icon itself** — not seen. Its presence is proven by the
   `RegisteredStatusNotifierItems` entry and the `Id="utter"` property (Step 1), not visually;
   the hidden-items popup was never opened.
3. **The portal shortcut dialog** — not reached. The app-id registration failed before any bind
   dialog, so the app reported the unbound hotkeys and took §6.3's manual-instructions path.
4. **The floating controller's own buttons** — the *Pause* button, the *Stop* button and the speed
   slider. The overlay is *visibly* on screen while speaking (the screenshot above), but I did not
   click it; pausing, resuming and stopping were driven through the socket as the brief's
   assignment directs.
5. **"Speak selection" reading the current selection** — the clipboard/selection path was not
   exercised; every utterance was given its text explicitly (`utter speak --text`).
6. **Audibility** — I have no ears here. Playback is evidenced by the live 24 kHz PipeWire
   sink-input and the engine's `audio=25.56` / `ttfirst` figures, not by listening.
7. **The tray *Quit* item** — likewise not reachable, so the app was stopped with `SIGTERM`. That
   skips the Qt shutdown path, so the socket file was left behind; it is stale (a connect probe
   gets `ECONNREFUSED`) and `utter status` correctly answers `not running`, which is the
   designed behaviour for a path left by a process that died:
   ```text
   $ uv run utter status
   utter: not running: no utter instance owns /run/user/1000/utter.sock     (exit 1)
   ```
8. **`utter doctor` and `utter hotkey --show`** — not exercised; they are Task 16's.

## Code and tests

**No code was changed.** Nothing had to be fixed: the two surprises (the brief's bus name and the
portal app-id) are a documentation mismatch and a designed non-fatal path respectively. No tests
were added and the suite was not re-run, because no Python file was touched — the only changes in
this commit are this report and its screenshot.

The app was stopped at the end of the session: PID 76735 is gone (`pgrep -fc "bin/utter"` → 0).
