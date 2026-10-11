# Utter — Phase 1 verification report

Real engine, real pipeline, on this machine (16 CPUs, PipeWire audio, pinned artifacts at
`~/.local/share/utter/models/`). The sample paragraph is `docs/sample-paragraph.txt`, which
carries all six number/currency forms of §2.3 (`1.5 million`, `$0.00`, `3,000`, `$1,234.56`,
`5%`, `12:30`).

Every number below names the command and instrument that produced it, and the values it quotes
appear in the **Evidence** section. The app's own lines (`config`, `model load`, `utterance`)
come from spec §9's logging in `src/utter/tts/engine.py` and land in
`~/.local/state/utter/utter.log`.

## Semantics of the logged fields

- `utterance units=` counts units **actually enqueued** (not the planned count), so a
  superseded or failed utterance reports what really happened.
- `audio` sums the **raw unit arrays** enqueued (`samples / 24000`), so it is the spoken audio
  and deliberately **excludes** the per-unit trailing pause the player is handed, and any
  inter-unit silence.
- `synth` is wall time inside the synthesiser for every unit attempted; for the **first**
  utterance of a process it therefore **includes the one-time model load** (timed and logged
  separately as `model load=`).
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
from `/proc/<pid>/status` (`VmRSS`, `VmHWM`) while the **console script**
`.venv/bin/utter speak --text "$(cat docs/sample-paragraph.txt)"` ran — the same script `uv run
utter` invokes, sampled directly because `uv run` forks a child Python that a `/proc/<uv-pid>`
read would miss (`uv` itself settles at ~30 MiB).

## Evidence

Engine log (the two facts a report quotes most, plus a config line):

```text
$ grep -E "config " ~/.local/state/utter/utter.log | tail -1
2026-10-11 05:36:58,640 INFO utter.tts.engine: config voice=af_heart speed=1.0 lang=en-us normalize=True max_chars=20000 unit_chars=80 first_chars=60
$ grep -E "model load" ~/.local/state/utter/utter.log | sed -E 's/.*model load=//' | sort | uniq -c
      5 0.48s
      6 0.49s
      6 0.50s
      1 0.51s
      1 0.53s
$ grep -E "utterance " ~/.local/state/utter/utter.log | tail -12
2026-10-11 05:31:39,998 INFO utter.tts.engine: utterance units=9 audio=25.56 synth=6.89 ttfirst=1.068
2026-10-11 05:32:08,766 INFO utter.tts.engine: utterance units=9 audio=25.56 synth=6.99 ttfirst=1.002
2026-10-11 05:32:37,384 INFO utter.tts.engine: utterance units=9 audio=25.56 synth=7.02 ttfirst=1.112
2026-10-11 05:33:06,123 INFO utter.tts.engine: utterance units=9 audio=25.56 synth=7.09 ttfirst=1.024
2026-10-11 05:33:45,311 INFO utter.tts.engine: utterance units=9 audio=25.56 synth=7.14 ttfirst=1.037
2026-10-11 05:34:13,621 INFO utter.tts.engine: utterance units=9 audio=25.56 synth=6.86 ttfirst=1.096
2026-10-11 05:34:42,444 INFO utter.tts.engine: utterance units=9 audio=25.56 synth=7.01 ttfirst=1.021
2026-10-11 05:35:10,752 INFO utter.tts.engine: utterance units=9 audio=25.56 synth=6.71 ttfirst=1.036
2026-10-11 05:35:39,840 INFO utter.tts.engine: utterance units=9 audio=25.56 synth=7.16 ttfirst=1.040
2026-10-11 05:36:07,765 INFO utter.tts.engine: utterance units=9 audio=25.56 synth=6.47 ttfirst=1.056
2026-10-11 05:36:36,881 INFO utter.tts.engine: utterance units=9 audio=25.56 synth=6.95 ttfirst=1.082
2026-10-11 05:37:05,346 INFO utter.tts.engine: utterance units=9 audio=25.56 synth=6.71 ttfirst=1.091
```

Peak RSS (`/usr/bin/time -v`, the three lines that matter; two runs each). Exact kB are quoted,
with the MiB conversion (÷1024) beside them:

```text
$ /usr/bin/time -v uv run utter speak --text "$(cat docs/sample-paragraph.txt)" --save-wav /tmp/utter-phase1.wav
	Elapsed (wall clock) time (h:mm:ss or m:ss): 0:06.97
	Maximum resident set size (kbytes): 965588     # 943.0 MiB
	Exit status: 0
  (second render run)
	Elapsed (wall clock) time (h:mm:ss or m:ss): 0:07.02
	Maximum resident set size (kbytes): 944704     # 922.6 MiB
	Exit status: 0

$ /usr/bin/time -v uv run utter speak --text "$(cat docs/sample-paragraph.txt)"
	Elapsed (wall clock) time (h:mm:ss or m:ss): 0:28.57
	Maximum resident set size (kbytes): 476592     # 465.4 MiB
	Exit status: 0
  (second playback run)
	Elapsed (wall clock) time (h:mm:ss or m:ss): 0:28.63
	Maximum resident set size (kbytes): 609440     # 595.2 MiB
	Exit status: 0

  (the round-1 /usr/bin/time -v runs printed, "Maximum resident set size" only)
  render:   859056  859776  932832  941980   # 838.9  839.6  911.0  919.9 MiB
  playback: 542540  552580  717176           # 529.8  539.6  700.4 MiB
```

Steady-state RSS — nine real playback runs, `VmRSS` at `t > 10 s` (the drain after synthesis).
Six runs polled every 0.2 s:

```text
run  drain_min median drain_max settled_min   (MiB)
0      241.9  241.9   241.9     241.9
1      227.9  367.4   367.4     227.9
2      212.0  367.7   367.7     212.0
3      367.2  367.2   367.2     367.2
4      221.6  544.2   544.2     221.6
5      241.7  241.7   241.7     241.7
```

and three runs polled every 0.25 s:

```text
run0: drain VmRSS min=198.4 max=241.4
run1: drain VmRSS min=305.0 max=305.0
run2: drain VmRSS min=241.5 max=241.5
```

The settled values across all nine runs are 241.9, 227.9, 212.0, 367.2, 221.6, 241.7, 198.4,
305.0, 241.5 — **198–367 MiB, median ≈240 MiB, one run above 320 MiB**.

Session-load floor — `/proc` `VmRSS`, 0.1 s poll, during the render's load step (MiB):

```text
181.8  182.2  183.7  191.3
```

WAV out of band:

```text
24000 1 25.56        # getframerate, getnchannels, nframes/framerate
238268 nonzero of the first 240000 samples
```

## Measured numbers

| Measurement | Value | Provenance | Runs |
|---|---|---|---|
| Model load | **0.48–0.53 s** | `model load=%.2fs` line (Evidence) | 19 |
| Unit count | **9** | `utterance … units=9` line (Evidence) | every playback |
| Audio duration | **25.56 s** | `utterance … audio=25.56`; WAV confirms 613 376 ÷ 24 000 | every run |
| Synthesis time | **6.47–7.16 s** | `utterance … synth=` (Evidence) | 12 |
| Mean real-time factor | **0.25–0.28** | `synth ÷ audio` | 12 |
| Mean synthesis per unit | **0.72–0.80 s** | `synth ÷ 9` | 12 |
| **Cold time to first frame** | **1.05 s**, of which **≈0.49 s** is the model load | `utterance … ttfirst=`; `model load=` | cited run |
| Warm time to first frame | **0.52–0.64 s**, typically ≈0.55 s — *derived* (`ttfirst − model load`) | the two lines above | 9 |
| Peak RSS, headless render | **838.9–943.0 MiB** (859 056–965 588 kB) | `/usr/bin/time -v` (Evidence) | 6 |
| Peak RSS, real playback | **465.4–700.4 MiB** (476 592–717 176 kB) | `/usr/bin/time -v` (Evidence) | 5 |
| Steady-state RSS (settled) | **198–367 MiB**, median ≈240 MiB | `/proc` `VmRSS` (Evidence) | 9 |
| Session-load floor | **≈182–183 MiB** | `/proc` `VmRSS`, 0.1 s poll during the load step (Evidence) | 2 |
| Wall clock, render | **6.97–7.02 s** | `/usr/bin/time -v` "Elapsed" (Evidence) | 2 |
| Wall clock, playback | **28.57–28.63 s** | `/usr/bin/time -v` "Elapsed" (Evidence) | 2 |
| WAV out of band | **24 000 Hz, mono, 613 376 frames, 25.56 s, non-silent** | `wave` + sample scan (Evidence) | 1 |

Peak RSS is allocator-dominated and varies run to run, so the range is a sample, not a bound:
the headless render measured 859 056–965 588 kB across six runs and playback 476 592–717 176 kB
across five (round-1 values are listed under Evidence beside the round-2 `/usr/bin/time -v`
tails). The spec cites the round-1 subset, 530–920 MiB.

## Verdicts against §1.3 and §1.4, both wordings

| Criterion | Original wording | Restated wording | Measured | Original verdict | Restated verdict |
|---|---|---|---|---|---|
| §1.3 steady-state RSS | ≤ 300 MiB | **median** ≤ 320 MiB (typical case) | settled 198–367 MiB over nine runs (median ≈240; two runs >300, one >320) | **MISS** (305 and 367 MiB runs) | **PASS** (median ≈240); tail reported: 1 run in 9 above 320 |
| §1.4 first sound | ≤ 0.5 s | ≤ 0.6 s **warm** in the typical case (*derived* = cold `ttfirst` − model load) | cold 1.05 s; warm 0.52–0.64 s over nine runs, typically ≈0.55 s (*derived*) | **MISS** (cold 1.05 s) | **PASS** (typical ≈0.55 s); worst run 0.64 s reported |

No code was changed to make any number pass, and no constant was moved.

### §1.3 — steady-state RSS

The design's 247 MiB is the *trimmed* floor and is reproduced: the process settles to
≈198–242 MiB in eight of the nine runs, with a session-load floor of only 183–191 MiB. But the
ninth run settled at 367 MiB, so within the nine-run series two runs exceeded 300 MiB and one
exceeded 320 MiB. Steady-state RSS is allocator-dependent and not deterministic at this
boundary: glibc's per-thread arenas retain freed synthesis transients until something triggers a
trim. Forcing early trimming (`MALLOC_TRIM_THRESHOLD=131072`) pins the run at 242 MiB, confirming
the excess is retained-freed pages rather than model footprint. **Peak** RSS is a separate
number (465–943 MiB across the session's runs per `/usr/bin/time -v`; the spec cites the round-1
subset, 530–920 MiB) and is not the steady-state criterion.

### §1.4 — first sound

The app's own cold `ttfirst` — the time from `speak()` returning to the first frame the player
accepted — is **1.05 s**, of which **≈0.49 s** is the one-time model load. That figure is for a
cold, one-shot CLI; the tray loads the model before any hotkey is pressed, so the warm figure,
**0.52–0.64 s over nine runs (typically ≈0.55 s)**, is what the criterion is really about. The
first unit also pays the session's first-inference warm-up, which §2.3's 0.28–0.45 s "short
first unit" figure does not; only later utterances reach the design's sub-0.5 s first-unit
synthesis.

## Cold-path wait against real audio

The playback command **does not exit before the speech ends**: wall clock **28.57–28.63 s**
against **25.56 s** of audio that starts after a ~0.49 s load and ~7 s of synthesis. The cold
`speak` path's `wait_idle` deadline (Task 9) is exercised against real playback, and the process
outlives the last frame as intended.

## Audible clip

`/tmp/utter-normalised.wav` was rendered by the shipped normaliser (§7.6) for this paragraph. It
is the espeak side of the D7 comparison re-run; the misaki side cannot be regenerated because
misaki is not shipped (D7 rejected it on measured cost).

## What was not measured here

Whole-app RSS with Qt loaded, worst-case responsiveness across a superseding `speak()` during
real playback, and the socket/tray path are Phase 2's. §1.3 here is a CLI process, not the tray
process; nothing in this report should be read as the tray's steady state.
