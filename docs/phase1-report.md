# Utter — Phase 1 verification report

Real engine, real pipeline, on this machine (16 CPUs, PipeWire audio, pinned artifacts at
`~/.local/share/utter/models/`). The sample paragraph is `docs/sample-paragraph.txt`, which
carries all six number/currency forms of §2.3 (`1.5 million`, `$0.00`, `3,000`, `$1,234.56`,
`5%`, `12:30`).

Every number below is traceable to a command whose output is pasted, and the provenance column
says which command and which instrument produced it. The app's own lines (`config`, `model
load`, `utterance`) come from spec §9's logging in `src/utter/tts/engine.py` and land in
`~/.local/state/utter/utter.log`.

## Semantics of the logged fields

- `utterance units=` counts units **actually enqueued** (not the planned count), so a
  superseded or failed utterance reports what really happened.
- `audio` sums the **raw unit arrays** enqueued (`samples / 24000`), so it is the spoken audio
  and deliberately **excludes** the per-unit trailing pause the player is handed, and any
  inter-unit silence.
- `synth` is wall time inside the synthesiser for every unit attempted; for the **first**
  utterance of a process it therefore **includes the one-time model load** (which is timed and
  logged separately as `model load=`).
- `ttfirst` is the time from `speak()` publishing the units to the first frame the player
  accepted; it is logged as `-` when nothing was accepted.

## Commands run

```bash
# headless render: WAV out of band plus peak RSS (the brief's mandated measurement)
/usr/bin/time -v uv run utter speak --text "$(cat docs/sample-paragraph.txt)" \
    --save-wav /tmp/utter-phase1.wav

# the real thing: playback through the actual output device
/usr/bin/time -v uv run utter speak --text "$(cat docs/sample-paragraph.txt)"

# WAV verified out of band
python3 -c "import wave; w=wave.open('/tmp/utter-phase1.wav'); \
    print(w.getframerate(), w.getnchannels(), w.getnframes()/w.getframerate())"
```

`/usr/bin/time -v` reports the maximum, not the steady state, so steady-state RSS was sampled
from `/proc/<pid>/status` (`VmRSS`, and `VmHWM` as a cross-check) while the **console script**
`.venv/bin/utter speak --text "$(cat docs/sample-paragraph.txt)"` ran — the same script
`uv run utter` invokes, sampled directly because `uv run` forks a child Python that a
`/proc/<uv-pid>` read would miss (`uv` itself settles at ~30 MiB). Sampling interval 0.2 s;
"settled" is the minimum `VmRSS` over the last ~3 s of the run, the drain after synthesis.

## The lines the app logged

```text
INFO utter.tts.engine: config voice=af_heart speed=1.0 lang=en-us normalize=True \
    max_chars=20000 unit_chars=80 first_chars=60
INFO utter.tts.engine: model load=0.49s
INFO utter.tts.engine: utterance units=9 audio=25.56 synth=6.85 ttfirst=1.109
```

## Measured numbers

| Measurement | Value | Provenance (command and instrument) | Runs |
|---|---|---|---|
| Model load | **0.48–0.51 s** (≈0.49) | `model load=%.2fs` line, render + playback | 6 |
| Unit count | **9** | `utterance … units=9` line | every playback run |
| Audio duration | **25.56 s** | `utterance … audio=25.56`; WAV 613 376 ÷ 24 000 confirms | every run |
| Synthesis time | **6.24–7.40 s** | `utterance … synth=` line | 9 |
| Mean real-time factor | **0.24–0.29** (mean ≈0.27) | `synth ÷ audio` | 9 |
| Mean synthesis per unit | **0.69–0.82 s** | `synth ÷ units` | 9 |
| Time to first frame, cold | **1.01–1.13 s** | `utterance … ttfirst=` line | 10 |
| Time to first frame, warm | **0.52–0.64 s** — *derived* (`ttfirst − model load`) | the two lines above | 9 |
| Peak RSS, headless render | **838.9–919.9 MiB** (859 056 / 859 776 / 932 832 / 941 980 kB) | `/usr/bin/time -v`, "Maximum resident set size" | 4 |
| Peak RSS, real playback | **529.8–700.4 MiB** (542 540 / 552 580 / 717 176 kB) | `/usr/bin/time -v` | 3 |
| Peak RSS cross-check | playback 480–736 MiB, render 914.1 MiB | `/proc` `VmHWM` polled every 0.2 s | 7 |
| Steady-state RSS (settled, drain) | **212–367 MiB**; typically ≈220–242, one run 367 | `/proc` `VmRSS`, 0.2 s interval, `t > 10 s` | 6 |
| Steady-state RSS (the three-run series the spec cites) | **198–305 MiB** | same method, 0.25 s interval | 3 |
| Session-load floor | **183–191 MiB** | `/proc` `VmRSS` first reading >150 MiB during the load step | 2 |
| Wall clock, render | **6.68–6.99 s** | `/usr/bin/time -v`, "Elapsed" | 4 |
| Wall clock, playback | **28.61–28.83 s** | `/usr/bin/time -v`, "Elapsed" | 6 |
| WAV out of band | **24 000 Hz, mono, 613 376 frames, 25.56 s, non-silent** (238 268 / 240 000 samples nonzero in the first 10 s) | `wave` + a sample scan | 1 |

Every MiB figure above is quoted in both the kB `/usr/bin/time`/`/proc` printed and the MiB it
converts to (÷1024): 859 056 kB = 838.9 MiB, 859 776 = 839.6, 932 832 = 911.0, 941 980 = 919.9;
542 540 kB = 529.8 MiB, 552 580 = 539.6, 717 176 = 700.4. Earlier drafts mis-quoted the low
ends (517 and 820 MiB); those were wrong and are gone. The `/usr/bin/time` series is
authoritative for peaks; `VmHWM` agrees for playback (480–736 MiB) and read 914.1 MiB for one
render against the series' 838.9–919.9 MiB, the difference being the coarser 0.2 s poll missing
the true instant of the peak.

## Verdicts against §1.3 and §1.4, both wordings

| Criterion | Original wording | Restated wording | Measured | Original verdict | Restated verdict |
|---|---|---|---|---|---|
| §1.3 steady-state RSS | ≤ 300 MiB | ≤ 320 MiB | settled 212–367 MiB (six runs; spec cites 198–305 MiB over three) | **MISS** — one run settled at 367 MiB; an earlier series reached 305 MiB | **PASS in 5 of 6** — but **MISS in 1 of 6** (367 MiB > 320 MiB) |
| §1.4 first sound | ≤ 0.5 s | ≤ 0.6 s **warm** (*derived* = measured cold `ttfirst` − model load) | cold 1.01–1.13 s; warm 0.52–0.64 s (*derived*) | **MISS** — every cold run ≥ 1.01 s | **PASS in most runs** (≈0.55 s) — but **MISS in some** up to 0.64 s |

No code was changed to make any number pass, and no constant was moved.

### §1.3 — steady-state RSS

The design's 247 MiB is the *trimmed* floor and is reproduced: the process settles to
≈198–242 MiB in most runs, with a session-load floor of only 183–191 MiB. But steady-state RSS
is allocator-dependent and not deterministic at the 300/320 MiB boundary: glibc's per-thread
arenas retain freed synthesis transients until something happens to trigger a trim, so
individual runs settle anywhere from 212 to 367 MiB. An early series settled at 198–305 MiB
(the spec's restatement cites it); a later six-run series settled at 212–367 MiB. Forcing early
trimming (`MALLOC_TRIM_THRESHOLD=131072`) pins the run at 242 MiB, confirming the excess is
retained-freed pages rather than model footprint. **Peak** RSS is a separate number (529.8–919.9 MiB
per `/usr/bin/time -v`) and is not the steady-state criterion.

### §1.4 — first sound

The app's own cold `ttfirst` — the time from `speak()` returning to the first frame the player
accepted — was 1.01–1.13 s on the real playback. That figure is for a cold, one-shot CLI, so it
includes the **~0.49 s one-time model load**; subtracting that load (the tray loads the model
before any hotkey is pressed) leaves **0.52–0.64 s** warm. The first unit also pays the
session's first-inference warm-up, which §2.3's 0.28–0.45 s "short first unit" figure does not.
On the shipped code the very first sound after a warm load is ≈0.55 s (the value the spec
cites) and only later utterances reach the design's sub-0.5 s first-unit synthesis.

## Cold-path wait against real audio

The playback command **does not exit before the speech ends**: wall clock **28.61–28.83 s**
against **25.56 s** of audio that starts after a ~0.49 s load and ~6.9 s of synthesis. The cold
`speak` path's `wait_idle` deadline (Task 9) is therefore exercised against real playback, and
the process outlives the last frame as intended.

## Audible clip

`/tmp/utter-normalised.wav` was rendered by the shipped normaliser (§7.6) for this paragraph. It
is the espeak side of the D7 comparison re-run; the misaki side cannot be regenerated because
misaki is not shipped (D7 rejected it on measured cost).

## What was not measured here

Whole-app RSS with Qt loaded, worst-case responsiveness across a superseding `speak()` during
real playback, and the socket/tray path are Phase 2's. §1.3 here is a CLI process, not the tray
process; nothing in this report should be read as the tray's steady state.
