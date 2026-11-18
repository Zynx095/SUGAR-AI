# SID — Voice System

Status labels as in `SID_UNIVERSAL_AI_ARCHITECTURE.md`. Measurements are from this machine
(RTX 5050 laptop GPU, 18 Nov 2026).

## Pipeline

```text
mic 16 kHz ─ Silero VAD ─ echo guard ─ endpointer ─ faster-whisper (small.en partials, large-v3-turbo final)
           ─ wake check ─ router …
model text stream ─ SpeechChunker ─ MeloTTS EN-Newest ─ OpenVoice V2 tone colour (SID) or DSP profile
                  ─ pitch/speed/volume offsets ─ trim + tighten pauses ─ crossfade join ─ speaker
```

## Voice identities — VERIFIED

`sugar/audio/voices.py` (catalog) and `sugar/audio/voice_profile.py` (sound). Median F0 measured with
pYIN on the same sentence:

| Id | Name | How it is made | Median F0 | Entity palette (core, glow, orbit, accent) |
|---|---|---|---|---|
| `sid` (default) | SID | MeloTTS, re-voiced by OpenVoice V2 as the speaker in `data/voice_samples/ULTRON_SID.mp3`; only pace and treble above 9.5 kHz fitted | 81 Hz (reference 83 Hz) | amber / gold / molten orange: `#ffab3d #f6cf7a #ff6a12 #ff4524` |
| `female` | SID (female) | MeloTTS EN-Newest speaker, light presence/air lift and compression; no pitch shifting, so it is a genuinely female voice, not a shifted male one | 205 Hz | crimson / rose / magenta: `#e8114f #ff6fa3 #9e0c5c #ffb03a` |
| `blue` | SID (blue) | MeloTTS English v1 **EN-AU** speaker; formants −8 %; time-varying PSOLA: intonation ×1.12, pitches pulled 60 % toward semitones; 1.6 ms comb; 80 Hz ring modulation at 25 %; 1.6× pauses | 138 Hz (range 17 st) | electric blue / ice / cobalt / cyan: `#1e90ff #c8f4ff #0b2a8f #00e5ff` |
| `sid_v2` | SID (mechanical) | the earlier processed DSP voice | — | Aether red |

Clone quality of `sid` (from `tools/fit_clone.py`): speaker similarity 0.89 to the reference (raw
MeloTTS 0.19), every band 60 Hz–16 kHz within 2.7 dB. Without the OpenVoice files SID falls back to the
DSP approximation `sid_dsp`.

Palettes are configurable per voice in `tts.palettes` (`sid.yaml`).

### Switching and adjusting — VERIFIED

Spoken or typed, parsed locally (no model): "switch to your female voice", "use the neutral voice",
"switch back to your original voice", "speak faster/slower", "make your voice deeper/higher",
"speak louder/quieter" (a bare "louder" is the system volume). Limits: speed 0.7–1.4 (step 0.08), pitch −6…+6 semitones (step 1.5, PSOLA),
volume 0.3–1.5 (step 0.2). Choices persist in `data/overrides.json` and survive restarts. The UI voice
list and the entity follow the same `voice.changed` event.

Live run: "switch to your female voice" → profile `female`, palette `#e8114f…`, reply in the new
voice; "switch back to your original voice" → `sid`, `#ffab3d…`. Sample saved to
`data/voice_samples/female_voice.wav`.

### Entity palette transition — VERIFIED (headless Edge)

`entity.js` `setPalette(colours, 800 ms)` tweens the core, glow, orbit and accent uniforms with a
smoothstep curve (spec: 500–1000 ms). The browser test checks a mid-transition frame (progress < 1)
and the final colours, and the UI accent colour follows.

The earlier third voice, `neutral` (the female EN-Newest speaker shifted down 3 semitones), measured 0.86
speaker-embedding similarity to the female voice. It was replaced by `blue` (0.39), and old settings that say
`neutral` map to `blue`. Details: [SID_DESKTOP_INTELLIGENCE.md](SID_DESKTOP_INTELLIGENCE.md#1-voices).

## Personalities — VERIFIED (superseded by the personality engine)

`sid`, `female`, `blue`, `professional`, `friendly`, `creative`, `cinematic`. Each sets the style prompt, tone
samples, reply length, temperature, speech pace and pauses. Each voice brings its own personality by default,
spoken changes are temporary ("be serious for a moment" lasts three answers) unless "from now on", and trait
dials adjust sarcasm, humour and brevity. See
[SID_DESKTOP_INTELLIGENCE.md](SID_DESKTOP_INTELLIGENCE.md#2-personality-engine).

## Speech flow — VERIFIED

- **First phrase early:** a long opening sentence is spoken from its first clause (comma, semicolon,
  colon or dash after at least 25 characters) instead of waiting for the full sentence; the second
  chunk may be short so audio keeps flowing while the model is still writing.
- Silence at chunk edges trimmed, internal pauses capped, chunks joined with short crossfades — no
  robotic gaps.
- Measured live (typed question, FreeLLMAPI): model first token 2482 ms, **TTS first audio 226 ms after
  the first phrase**, typed-text-to-voice 2908 ms. With the local model the first token arrived in 304 ms
  (warm).
- A test streams answers token by token and checks no word is lost or duplicated.

## Wake phrase

### Rules — VERIFIED

"Hey SID" at the start; a bare "SID," at the start; "…, SID." set off at the end. Whisper's mishearings
are handled in two tiers:

1. **Exact** — the transcript contains the name (`sid`, `syd`, `sidd`, `sydd`, `cid`, or spelled
   "S.I.D."): wake.
2. **Look-alike** — "He said", "Hay said", "Haced", "Hesed", "acid", "hey side", "hey sit": these
   are common in ordinary speech, so SID **re-listens**: a second Whisper pass over the first 2.2 s with
   a prompt biased toward "Hey SID". It wakes only if the name appears in the first four words.
   `conversation.wake_verify: true`. Events `wake.detected {kind, verified}` / `wake.rejected`, both
   counted in telemetry.

Calibration on synthesised speech (natural voice and SID's own voice, 20 clips):

| Said | Heard | Decision |
|---|---|---|
| "Hey Sid, run the tests." | "Hey, Sid, run the test." | wake (exact) |
| "Hey Sid, what is a closure?" (SID voice) | "Hay said, what is a closure?" | re-listen heard "hey SID" → wake |
| "Sid, open the project." | "Sid opened the project." | wake (exact) |
| "He said the build is failing." | same | re-listen → **rejected** |
| "That is exactly what he said." | same | **rejected** |
| "Hey, sit down for a minute." | same | **rejected** |
| "I said we should leave early." / "Hey, can you pass the salt?" | same | not a wake form |

One miss: SID-voice "Hey Sid, run the tests." was heard as "Haced run the tests." — "haced" was then
added to the look-alike forms (re-listen decides). Verification cost: one extra small.en pass on ≤2.2 s
of audio, only for look-alike transcripts.

### Live microphone test — IMPLEMENTED, **pending your run**

`tools/wake_test.py` prompts you through 10 wake phrases and 10 sound-alike control sentences, records
each with your microphone, runs the same logic, and saves detections, misses, false activations,
confidence and latency to `data/wake_tests/<time>.json`:

```powershell
.\venv\Scripts\python.exe tools\wake_test.py
```

I did not run it: it needs your voice and your room. Real-voice accuracy is therefore **not verified**.

### openWakeWord — evaluated, NOT IMPLEMENTED

openWakeWord would give an always-on, low-CPU detector with a tunable threshold, but there is no
pretrained "Hey SID" model; a custom one needs its training pipeline (thousands of synthetic positives
from a TTS model plus large negative audio sets, a few GPU hours) and per-room threshold tuning. Given
that the Whisper path already runs continuously for barge-in and transcription, the two-tier
Whisper check is the cheaper improvement now. Recommended next step if false wakes or misses show up in
`wake_test.py`: train an openWakeWord model on D:, run it as a gate in front of Whisper, keep the
re-listen as the second stage.

## Status summary

| Feature | Status |
|---|---|
| Cloned SID voice | VERIFIED |
| Female and neutral identities | VERIFIED (F0 measured, switching live) |
| Voice-linked palettes with interpolation | VERIFIED |
| Speed / pitch / volume adjustments, persistence | VERIFIED |
| Personalities independent of voice | VERIFIED |
| Streaming first phrase | VERIFIED |
| Wake verification (look-alikes) | VERIFIED on synthetic speech; real-mic test pending |
| openWakeWord | NOT IMPLEMENTED (evaluated) |
| Acoustic echo cancellation | NOT IMPLEMENTED (no AEC in PortAudio on Windows; headset recommended) |
