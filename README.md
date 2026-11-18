# SID

A personal AI for Windows that you talk to or type to. SID answers questions, writes, teaches, does
research, role-plays, helps with health questions responsibly, operates your PC, and does software
engineering through **Claude Code** in the project you choose. Its presence is a real-time WebGL
particle entity whose motion follows SID's real state and whose colours follow its voice, and it speaks
in a voice cloned from `data/voice_samples/ULTRON_SID.mp3`.

Detailed documents: [architecture](SID_UNIVERSAL_AI_ARCHITECTURE.md) ·
[security](SID_SECURITY_AUDIT.md) · [voice](SID_VOICE_SYSTEM.md) ·
[provider routing](SID_PROVIDER_ROUTING.md) · [storage and offline](SID_STORAGE_AND_OFFLINE_REPORT.md) ·
[acceptance results](SID_FINAL_ACCEPTANCE_REPORT.md).

```text
mic → VAD → endpointer → Whisper → "Hey SID" check ─┐
typed text ─────────────────────────────────────────┤
                                                    ▼
      router: self-commands → grammar commands → engineering classifier → 20 universal categories
                                                    ▼
   answer (model + category tools) │ Claude Code (explicit project) │ desktop agent │ command
                                                    ▼
      every tool call → security policy (who asked, how risky, was untrusted content read?)
                                                    ▼
      streamed text → first phrase early → MeloTTS → OpenVoice clone → speaker
```

## Status at a glance

Labels: **VERIFIED** = exercised on this machine with real models/CLI/browser; **IMPLEMENTED** = code
and automated tests; **NOT IMPLEMENTED**; **PENDING** = needs a run only you can do.

| Feature | Status |
|---|---|
| Conversation, knowledge, creative writing, roleplay, coding questions | VERIFIED |
| Education, maths, research, productivity, document creation | IMPLEMENTED |
| Health information (emergencies, local-only personal data, no diagnosis) | VERIFIED |
| Claude Code builds and analysis in an explicit project | VERIFIED |
| Desktop, browser, YouTube/Spotify, files, PowerShell | IMPLEMENTED (live suite not re-run this pass) |
| Prompt-injection protection, command risk policy | VERIFIED |
| Online → local fallback, offline mode with Ollama on D: | VERIFIED |
| Voices (SID, female, neutral), palettes, personalities | VERIFIED |
| Wake-phrase look-alike verification | VERIFIED on synthetic speech, real-mic test PENDING |
| Telemetry history and export | IMPLEMENTED (UI VERIFIED) |
| CI workflow | IMPLEMENTED, first GitHub run PENDING (needs a push) |
| Image generation | NOT IMPLEMENTED (SID writes a prompt for an image tool instead) |
| Internal `sugar` → `sid` package rename | NOT IMPLEMENTED (deferred, migration plan in the architecture doc) |

## What SID does

- **Talks and answers** — general conversation and knowledge, explanations, tutoring, maths with a
  calculator, research with web lookups (public sites only), stories and poems, documents written to
  disk, roleplay in its own context that never leaks into normal conversation.
- **Health questions, carefully** — emergencies get "Call 112" immediately without a model;
  anything with your own values ("my haemoglobin is 9.8") goes only to the local model unless you say
  "allow online health answers"; it never diagnoses or prescribes.
- **Engineering through Claude Code** — "fix the login bug in Jiva", "create a React app in
  D:\College\shop", "analyse this project". Claude Code runs only in a project you selected, named or
  typed. No project → SID asks. Analysis is read-only. Progress, tests and costs stream to the Workbench.
  Ordinary conversation never reaches Claude Code.
- **Operates the PC** — apps and windows, typing and shortcuts, the browser, YouTube and Spotify search,
  playback, files, clipboard, screenshots, PowerShell — within `permissions.allowed_roots`.
- **Stops sensibly** — "stop" stops speech, then the running answer or action; if only Claude Code is
  working it asks before stopping it; with nothing running it pauses your media.

## Safety in one table

| Who asked | Read | Small change | Modify | Unknown / network / destructive / privileged | Credentials |
|---|---|---|---|---|---|
| You, directly | runs | runs | runs | asks first | refused |
| The model on your behalf | runs | runs (level ≥ 1) | runs (level ≥ 2) | asks first | refused |
| The model after reading a web page, file, email… | runs | asks | asks | **refused** | refused |

Secrets are redacted before they reach chat, speech or a model. Claude Code can't force-push, format,
edit the registry or shut down, and can't delete recursively or rewrite history unless you confirmed a
destructive request. Details: [security audit](SID_SECURITY_AUDIT.md).

## Voices and personalities

| Say | Effect |
|---|---|
| "switch to your female voice" / "use the neutral voice" / "switch back to your original voice" | voice + entity colours (amber → crimson → cyan), 0.8 s transition |
| "speak faster", "make your voice deeper", "speak quieter" | adjusts and remembers |
| "be more professional", "be friendly", "be cinematic", "go back to your normal personality" | how SID writes, independent of the voice |

Wake phrase **"Hey SID"** (or "SID, …" at the start). Transcripts that only look like it ("He said…")
are re-checked before SID wakes.

## Requirements

- Windows 10 (1809+) or 11, Python 3.12, an NVIDIA GPU recommended (tested on an RTX 5050 laptop GPU)
- [Claude Code](https://claude.com/claude-code) (`claude` on PATH) for engineering work
- [FreeLLMAPI](https://freellmapi.co/) for online answers; Ollama (portable, on D:) for offline
- Node.js for the UI unit tests; Microsoft Edge for the browser UI test

## Setup

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install torch==2.7.1 --index-url https://download.pytorch.org/whl/cu128
.\venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env            # FREELLMAPI_API_KEY
copy sid.example.yaml sid.yaml    # optional (a legacy sugar.yaml is still read)
git config core.hooksPath .githooks
```

Models live under `D:\Sugar_Models` (Whisper, Hugging Face cache, OpenVoice, Ollama); nothing large goes
to C:. See [storage and offline](SID_STORAGE_AND_OFFLINE_REPORT.md).

## Running

```powershell
.\venv\Scripts\python.exe main.py                 # voice + UI window
.\venv\Scripts\python.exe main.py --no-voice      # type only
.\venv\Scripts\python.exe main.py --text "explain how black holes work"   # one-shot
$env:SID__LLM__FREELLM__ENABLED="false"; $env:SID__LLM__CLAUDE__ENABLED="false"
.\venv\Scripts\python.exe main.py                 # offline: local model only
```

`` ` `` opens telemetry (latency, engineering, health, events, **history** with CSV/JSON export),
`Ctrl+M` toggles the microphone, `Esc` stops SID.

## Configuration

Defaults → `sid.yaml` → `data/overrides.json` (UI and voice changes) → environment `SID__SECTION__KEY`
(legacy `SUGAR__` too). Useful keys: `conversation.scope` (`universal` / `engineering_only`),
`conversation.personality`, `conversation.wake_verify`, `conversation.health_external`,
`tts.voice_profile`, `tts.palettes`, `llm.routes`, `llm.ollama.*`, `permissions.auto_approve_level`,
`permissions.allowed_roots`, `coding.project_roots`, `telemetry.retention_days`.

## Tests

```powershell
.\venv\Scripts\python.exe -m pytest --ignore=tests\test_desktop_live.py      # 546 tests, ~75 s
node --test tests\ui\*.test.mjs                                               # UI units
$env:SUGAR_MODEL_TESTS=1; .\venv\Scripts\python.exe -m pytest tests\test_stt.py tests\test_tts.py
$env:SUGAR_DESKTOP_TESTS=1; .\venv\Scripts\python.exe -m pytest tests\test_desktop_live.py   # takes focus
.\venv\Scripts\python.exe tools\wake_test.py                                  # your microphone
```

CI (`.github/workflows/ci.yml`) runs lint, config validation, UI and Python tests on Windows with no
secrets. The pre-commit hook runs the fast safety and routing tests in about 12 s.

## Project layout

```text
sugar/           (internal package name kept; user-facing name is SID)
  app/           application wiring (SIDApp), self tools (voice, personality, health consent)
  security/      provenance, command risk classes, policy
  intelligence/  router, categories, health, personalities, orchestrator, conversation, context
  coding/        project registry, Claude Code runner and sessions
  providers/     FreeLLMAPI, Ollama (+ managed server), Claude; pool and capability routing
  agent/         agent loop, permissions, tool executor
  audio/         capture, VAD, STT, TTS, voice clone, voice profiles and identities
  computer/      desktop control
  core/          events, state machine, metrics, telemetry store, logging
  ui/            WebSocket bridge, window host, web UI (entity, workbench, telemetry)
melo/, openvoice/  vendored MeloTTS and OpenVoice (MIT)
tools/           voice fitting, wake test
```

## Known limitations

- Classification is rule-based; unusual phrasing can land in a neighbouring category. It fails toward
  answering or asking, never toward executing.
- The local model (`qwen2.5:3b`) is small; offline answers are fast but simpler.
- No acoustic echo cancellation on Windows/PortAudio; a headset gives the best barge-in.
- No image generation model.
