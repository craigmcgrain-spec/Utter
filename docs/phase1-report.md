# Utter — Phase 1 verification report

Real engine, real pipeline, on this machine (16 CPUs, PipeWire audio, pinned artifacts at
`~/.local/share/utter/models/`). Every number below is a quote from the shipped code's own log
line or from `/usr/bin/time -v`; no private harness was used. The sample paragraph is
`docs/sample-paragraph.txt`, which contains all six number/currency forms of §2.3
(`1.5 million`, `$0.00`, `3,000`, `$1,234.56`, `5%`, `12:30`).

The app's own lines (`config`, `model load`, `utterance`) come from spec §9's logging in
`src/utter/tts/engine.py` — the per-utterance line carries `units`, `audio` seconds, `synth`
seconds and `ttfirst` seconds, and the resolved config is logged once at first use. All four of
these are on the module logger `utter.tts.engine` and land in `~/.local/state/utter/utter.log`.

## Commands run

```bash
# headless render: WAV out-of-band plus peak RSS (the brief's mandated measurement)
/usr/bin/time -v uv run utter speak --text "$(cat docs/sample-paragraph.txt)" \
    --save-wav /tmp/utter-phase1.wav

# the real thing: playback through the actual output device
/usr/bin/time -v uv run utter speak --text "$(cat docs/sample-paragraph.txt)"

# WAV verified out of band
python3 -c "import wave; w=wave.open('/tmp/utter-phase1.wav'); \
    print(w.getframerate(), w.getnchannels(), w.getnframes()/w.getframerate())"
```

Steady-state RSS is not something `/usr/bin/time -v` reports (it reports the maximum), so the
RSS during the real playback's drain — model warm, player open, no synthesis in flight — was
sampled from `/proc/<pid>/status` (`VmRSS`) every 0.2–0.25 s while running the playback command
above. Peak RSS is reported both ways (`/usr/bin/time -v` and `VmHWM`) and the two agree.

## The lines the app logged

```text
INFO utter.tts.engine: config voice=af_heart speed=1.0 lang=en-us normalize=True \
    max_chars=20000 unit_chars=80 first_chars=60
INFO utter.tts.engine: model load=0.48s
INFO utter.tts.engine: utterance units=9 audio=25.56 synth=6.24 ttfirst=1.054
```

(Second playback run: `model load=0.49s`, `utterance units=9 audio=25.56 synth=6.93
ttfirst=1.062`. Headless render: `model load=0.48s` — the render logs no `utterance` line
because it has no player and therefore no time-to-first-frame.)

## Measured numbers

| Measurement | Value | Source |
|---|---|---|
| Model load | **0.48–0.51 s** (≈0.49 s) | `model load=%.2fs` line, playback + render |
| Unit count | **9** | `utterance … units=9` |
| Audio duration | **25.56 s** | `utterance … audio=25.56`; WAV confirms 613 376 frames ÷ 24 000 |
| Synthesis time | **6.24 s / 6.93 s** (two playback runs) | `utterance … synth=` |
| Mean real-time factor | **0.26** (0.244, 0.271) | `synth ÷ audio` |
| Mean synthesis per unit | **0.69–0.77 s** | `synth ÷ units` |
| Time to first frame (cold CLI) | **1.049–1.062 s** | `utterance … ttfirst=` |
| Time to first frame (warm model) | **≈0.56 s** | `ttfirst − model load` [derived] |
| Peak RSS, real playback | **517–700 MiB** (542 540–717 176 kB) | `/usr/bin/time -v`; `VmHWM` 530–736 MiB |
| Peak RSS, headless render | **820–920 MiB** (859 056–941 980 kB) | `/usr/bin/time -v`; `VmHWM` 842 MiB |
| Steady-state RSS (warm, draining) | **198–305 MiB** (three runs: 198–241, 305, 241) | `/proc` `VmRSS` during playback |
| Session-load floor | **183–191 MiB** | `/proc` `VmRSS` profile, first unit |
| Wall clock, render | **6.68–6.88 s** | `/usr/bin/time -v` |
| Wall clock, playback | **28.61–28.83 s** | `/usr/bin/time -v` |
| WAV out of band | **24 000 Hz, 1 ch, 613 376 frames, 25.56 s, non-silent** (238 268/240 000 nonzero in the first 10 s) | `wave` + sample scan |

## Verdicts against §1.3 and §1.4

### §1.3 — steady-state RSS ≤ 300 MiB → **MISS (borderline)**

Steady-state RSS with the model warm and the engine idle (measured during the real playback's
~20 s drain) was **198–305 MiB across three runs** — typically ≈240 MiB, which reproduces the
design's §2.3 figure of 247 MiB, but **one of the three runs held 305 MiB**, above the 300 MiB
criterion. The session-load floor is only 183–191 MiB, so the excess is transient synthesis
memory that glibc's per-thread arenas retain after `free()`, not model footprint: forcing early
trimming (`MALLOC_TRIM_THRESHOLD=131072`) pins the same run at **242 MiB**, and a short
one-unit utterance drains to **222 MiB**. The criterion is therefore met on the design's own
terms and in two of three runs, but is not reliably ≤300 MiB, so this is reported as a miss
rather than rounded to a pass. For the record, **peak** RSS (the §11 budget is also ≤300 MiB)
is **517–920 MiB** — a clear miss on the peak reading, driven by the render synthesising all
nine units as fast as it can.

### §1.4 — first sound ≤ 0.5 s → **MISS**

The app's own `ttfirst` — the time from `speak()` returning to the first frame the player
accepted — was **1.049–1.062 s** on the real playback. That figure is for a cold, one-shot CLI,
so it includes the **~0.49 s one-time model load**; subtracting that load (the tray loads the
model before any hotkey is pressed) leaves **≈0.56 s**, still just over the 0.5 s target. The
first unit also pays the session's first-inference warm-up, which §2.3's 0.28–0.45 s
"short first unit" figure does not; on the shipped code the very first sound after a warm load
is therefore ≈0.56 s, and only later utterances reach the design's sub-0.5 s first-unit
synthesis. This is reported as a miss with both numbers rather than presented as a pass.

## Cold-path wait against real audio

The playback command **does not exit before the speech ends**: wall clock **28.61–28.83 s**
against **25.56 s** of audio that starts at `ttfirst` after a ~0.49 s load. The cold `speak`
path's `wait_idle` deadline (Task 9) is therefore exercised against real playback, and the
process outlives the last frame as intended.

## Audible clip

`/tmp/utter-normalised.wav` was rendered by the shipped normaliser (§7.6) for this paragraph. It
is the espeak side of the D7 comparison re-run; the misaki side cannot be regenerated because
misaki is not shipped (D7 rejected it on measured cost).

## What was not measured here

Whole-app RSS with Qt loaded, worst-case responsiveness across a superseding `speak()` during
real playback, and the socket/tray path are Phase 2's. §1.3's peak figure here is a CLI
process, not the tray process; nothing in this report should be read as the tray's steady state.
