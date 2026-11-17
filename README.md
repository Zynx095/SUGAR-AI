# SID

A software-engineering intelligence for Windows. SID answers programming questions, inspects
repositories and builds software, with **Claude Code** as its execution engine. You talk to it or type to
it; it routes every request through an engineering boundary, runs real work in the project you chose, and
reports what actually happened. Its presence is a real-time WebGL particle entity whose motion follows
SID's real state, and it speaks in an original, synthetic voice.

```text
mic → Silero VAD → echo guard → endpointer ─┐
                                            ├→ Whisper ─┐
                          typed input ──────┘           ▼
                                        engineering classifier (local, deterministic)
                     ┌───────────────┬──────────┴─────────┬──────────────────┐
              NON_ENGINEERING     CODING_QA        PROJECT_ANALYSIS     BUILD_EXECUTION
              fixed refusal     model, no tools   Claude Code, plan     Claude Code in the
              (no model, no                        mode (read-only)      chosen project
               tool, no CLI)          │                    └──────┬───────────┘
                                      ▼                           ▼
             speaker ← SID voice profile ← MeloTTS ← speech chunker ← streamed text / progress
```

## What SID does

- **Answers programming questions** (concepts, debugging, code interpretation, algorithms) through the
  configured model chain (FreeLLMAPI, then Ollama or Claude). The model gets no tools in this path, so an
  answer can't change anything.
- **Analyses repositories** through Claude Code in `plan` mode: architecture, dependencies, audits,
  "why is the build failing".
- **Builds and changes software** through the real `claude` CLI: new applications, features, fixes,
  refactors, tests. Progress streams into the Workbench and the conversation; SID speaks a short summary
  when Claude Code finishes, including test results and unresolved issues.
- **Refuses everything else** — weather, jokes, music, reminders, web searches, desktop automation —
  with one sentence: "I'm designed for software engineering and development. I can't assist with that
  request." Refused requests never reach a model, a tool or Claude Code. Voice and typing share the same
  classifier, so typing can't get around it.

### Desktop control

`conversation.desktop_control` (on by default) lets SID operate the PC as well: apps and windows, typing
and shortcuts, the browser on your screen, **YouTube and Spotify search** ("type lofi beats in YouTube",
"search Spotify for Daft Punk" opens the Spotify app on its search page), playback, files, the clipboard,
screenshots and PowerShell. Commands the grammar understands run directly; anything else goes to the
agent model with the full computer toolset. Small talk and general-knowledge questions are still refused.
`permissions.auto_approve_level: 2` lets sensitive actions (PowerShell commands, installs, force-closing
apps) run without asking; destructive actions (deleting files, wiping data) are always confirmed first.
File and terminal access is limited to `permissions.allowed_roots`.

### The engineering boundary

`sugar/intelligence/domain.py` classifies intent, not keywords:

| Request | Category |
|---|---|
| "What is Python?" | CODING_QA |
| "What is the weather in Python?" | NON_ENGINEERING |
| "Create a Python application" | BUILD_EXECUTION |
| "Tell me a joke about Java" | NON_ENGINEERING |
| "Build a weather app in React" | BUILD_EXECUTION |
| "Analyse the architecture of this project" | PROJECT_ANALYSIS |
| "Fix it" with no engineering work in context | ambiguous → SID asks, nothing runs |

### Project isolation

- SID starts every run with **NO ACTIVE PROJECT**. The previous session's project is remembered only as
  "previous", never presented as current.
- Claude Code runs only in a project you selected this session ("open Jiva"), a project named in the
  request ("fix the login bug in Jiva"), or a directory you typed ("create a React app in
  D:\College\shop"). Typed directories must be inside `permissions.allowed_roots`.
- With no project, SID asks which one and resumes the request when you answer with a name.
- Switching projects updates the header, the Workbench and Claude Code's working directory together.

### Safety

- Analysis runs read-only (`plan` mode).
- `git commit`, `git push` and `git merge` are disallowed unless the request asks for them; force pushes,
  disk formatting, registry edits and shutdown never run.
- Recursive deletes, `git clean`, `reset --hard`, history rewrites are disallowed unless you confirmed a
  destructive request. Destructive requests ("delete the build folder") need a spoken or clicked
  confirmation before Claude Code starts.
- API keys, tokens and `KEY=value` lines are redacted from chat, speech and the Workbench.
- Every Claude Code run has a timeout (`coding.task_timeout_s`, 30 minutes by default).

### Voice

- Wake phrase **"Hey SID"**; a bare "SID" at the start ("SID, run the tests") or set off at the end
  ("run the tests, SID.") also works. Whisper's common mishearings ("Hey side") count only after "hey",
  so ordinary sentences don't wake it.
- The voice is cloned from `data/voice_samples/ULTRON_SID.mp3`: MeloTTS speaks, and OpenVoice V2's tone
  colour converter (vendored in `openvoice/`, MIT; model in `D:\Sugar_Models\OpenVoiceV2`) re-voices it
  as the reference speaker. Only the pace and the treble above 9.5 kHz are fitted (`tools/fit_clone.py`).
  Measured: speaker similarity 0.89 to the reference (raw MeloTTS 0.19), median pitch 81 Hz (reference
  83 Hz), every band 60 Hz–16 kHz within 2.7 dB. Without the model files SID falls back to a DSP
  approximation (`sid_dsp`, `tools/fit_voice.py`).
- Barge-in is unchanged: talk over SID and it stops within one audio block.

### Interface

- A Three.js (r169, vendored) particle entity: emissive core, five inclined orbits with travelling
  energy packets, a 14,000-particle field and horizontal streams. Its states — awaiting input,
  listening, analysing, engineering, executing, speaking, interrupted, fault, offline, microphone off —
  come only from real events. It stops rendering when the window is hidden and falls back to a Canvas 2D
  version without WebGL.
- **Engineering Workbench**: project, directory, Claude Code status, task, live steps, test and build
  results (from the commands' exit status), duration, session id, cost and tokens only when Claude Code
  reports them. It stays a slim rail while idle and opens while a task runs.
- **Telemetry** (`` ` ``): per-turn latency (end of speech, STT, LLM first token, TTS first audio, end to
  end), Claude Code process and session, last provider and route, measured event-bus rate, component
  health, renderer frame rate.

## Requirements

- Windows 10 (1809+) or 11, Python 3.12
- An NVIDIA GPU is strongly recommended (tested on an RTX 5050 laptop GPU).
- [Claude Code](https://claude.com/claude-code) installed (`claude` on PATH) for analysis and build work
- [FreeLLMAPI](https://freellmapi.co/) running for programming questions; optional
  [Ollama](https://ollama.com/) as the offline fallback
- Node.js only to run the UI unit tests; Microsoft Edge for the browser UI test

## Setup

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install torch==2.7.1 --index-url https://download.pytorch.org/whl/cu128
.\venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env            # FREELLMAPI_API_KEY
copy sid.example.yaml sid.yaml    # optional (a legacy sugar.yaml is still read)
```

Models download on first run (Whisper into `D:\Sugar_Models\Whisper`, MeloTTS into the Hugging Face cache).

## Running

```powershell
.\venv\Scripts\python.exe main.py                 # voice + UI window
.\venv\Scripts\python.exe main.py --window edge   # UI in an Edge app window
.\venv\Scripts\python.exe main.py --no-voice      # type only
.\venv\Scripts\python.exe main.py --text "what is a closure in Python?"   # one-shot, prints the reply
```

`` ` `` opens telemetry, `Ctrl+M` toggles the microphone, `Esc` stops SID.

## Things to say

| You say | What happens |
|---|---|
| "Hey SID." | "Yes?" |
| "Why does this Python function return None?" (with the code) | answered in the conversation, code highlighted |
| "Open my Jiva project." | Jiva becomes the active project; Claude Code is linked to it |
| "Analyse the architecture of this project." | read-only Claude Code analysis, summary spoken |
| "Fix the authentication bug." | Claude Code works in Jiva; steps and test results stream into the Workbench |
| "Create a React application with authentication in D:\College\shop" | the directory is created, Claude Code scaffolds it |
| "Can you build me a website?" | SID asks for the target directory and requirements |
| "Are you done?" / "What is Claude doing?" | status of the running task, or "No engineering task is active." |
| "Pause the coding session." / "Continue." / "Show me what changed." | session control, git diff |
| "Delete the node_modules folder." | asks for confirmation first |
| "Remember that this project uses pnpm." | engineering memory |
| "Wait." / "Stop." / "Go to sleep." | interruptions |
| "Play some music." / "What's the weather?" | refused |

## Project layout

```text
sugar/           (internal package name kept from the previous version)
  app/           application wiring and lifecycle (SIDApp)
  audio/         capture, VAD, endpointing, echo guard, STT, TTS, voice profile, playback
  intelligence/  engineering classifier, router, orchestrator, conversation manager, context, working memory
  coding/        project registry, Claude Code CLI runner, coding sessions
  providers/     FreeLLMAPI (OpenAI-compatible), Ollama, Claude (API / CLI), fallback pool
  agent/         agent loop, permissions, tool executor
  tools/         coding and memory tools (desktop/media tools remain in the code but no route reaches them)
  computer/      legacy desktop control (disabled by default: computer.backend = none)
  memory/        SQLite + FTS5 store
  ui/            WebSocket bridge, window host, web UI (entity, workbench, telemetry)
  config/        layered settings
  core/          events, state machine, metrics, logging, processes
melo/            vendored MeloTTS (English)
tests/           Python suite, tests/ui (Node unit tests), headless-Edge UI test, fake Claude CLI
main.py          entry point
```

## Configuration

Defaults → `sid.yaml` (or legacy `sugar.yaml`) → `data/overrides.json` (UI changes) → environment
`SID__SECTION__KEY` (legacy `SUGAR__` too). Secrets come only from the environment / `.env`. Relevant
keys: `coding.project_roots`, `coding.task_timeout_s`, `coding.permission_mode`,
`permissions.allowed_roots`, `tts.voice_profile`, `conversation.wake_words`. See `sid.example.yaml`.

## Tests

```powershell
.\venv\Scripts\python.exe -m pytest --ignore=tests\test_desktop_live.py   # full suite (includes UI tests)
node --test tests\ui\*.test.mjs                                            # UI units on their own
$env:SUGAR_MODEL_TESTS=1; .\venv\Scripts\python.exe -m pytest tests\test_stt.py tests\test_tts.py
```

`tests/test_claude_runner_process.py` drives the real subprocess code against `tests/fake_claude.py`
(streaming, resume, per-project working directory, cancellation, timeout, crash, failing tests).
`tests/test_ui_browser.py` loads the shipped page in headless Edge and drives it over the DevTools
protocol; screenshots go to `data/ui_screens/`.

## Known limitations

- The engineering classifier is rule-based. It is tested on a broad set of phrasings, but an unusual
  phrasing can land in the wrong category; it fails safe (a refusal or a question, never execution).
- MeloTTS has one English speaker and no pitch control, so SID's voice is shaped after synthesis. It is
  deeper and synthetic, but it is not a studio-designed voice.
- No acoustic echo cancellation is available to PortAudio on Windows; a headset gives the best barge-in.
- The Python package is still named `sugar` internally to avoid a repository-wide import rename.
- The legacy desktop-control modules and their tests remain in the repository but are unreachable from
  SID's routing.
