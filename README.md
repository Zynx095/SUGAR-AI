# Sugar

A voice-first assistant for Windows. Sugar listens continuously, answers while it is still thinking,
stops the moment you talk over it, operates your computer through permission-checked tools, and hands
real software work to **Claude Code** so you can build things hands-free.

```text
mic → Silero VAD → echo guard → endpointer ─┐            ┌→ fast-path tools (no model)
                                            ├→ Whisper ──┤→ FreeLLMAPI / Ollama / Claude (+ tools)
         speaker ← MeloTTS ← speech chunker ┘            └→ Claude Code sessions (real CLI)
```

## What it does

- **Continuous listening** with a wake word ("Sugar") and a follow-up window, so a conversation does
  not need the wake word on every turn.
- **Natural turn-taking**: a pre-roll buffer keeps your first syllable, and the pause Sugar waits before
  answering depends on what you said ("open Chrome" ends fast, "I want you to…" waits).
- **Streaming end to end**: model tokens become sentences, sentences become audio while the rest is
  still being generated. Code, tables, paths and stack traces are shown, not read aloud.
- **Interruptions**: talk over Sugar and it ducks, then stops within one audio block. "Wait" gets a
  "Yeah?", "stop" cancels whatever is running.
- **Instant commands** without a model: apps, volume, media/Spotify, time, weather, typing,
  screenshots, projects, Claude Code control.
- **Model routing**: FreeLLMAPI for conversation, Ollama as the offline fallback, Claude for deep
  reasoning (API key or your Claude subscription through the CLI).
- **Claude Code integration**: open a project by name or spoken path ("the project in D colon College
  JIVA"), delegate analysis or fixes, hear progress and a spoken summary, pause/resume across restarts,
  approve the actions Claude Code asks for by voice. Commits only happen when you ask for one.
- **Safety model**: read / non-destructive / sensitive / destructive levels, spoken confirmation for
  risky actions, file access confined to allowed folders, deletes go to the Recycle Bin.
- **Memory**: conversation history plus explicit long-term memories ("remember that I prefer Python"),
  searchable, listable and deletable.

## Requirements

- Windows 10/11, Python 3.12
- An NVIDIA GPU is strongly recommended (tested on an RTX 5050 laptop GPU). CPU works with smaller
  Whisper models and slower speech.
- [FreeLLMAPI](https://freellmapi.co/) desktop app running (default chat provider)
- Optional: [Ollama](https://ollama.com/) for offline answers, [Claude Code](https://claude.com/claude-code)
  for coding sessions, Spotify developer credentials

## Setup

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install torch==2.7.1 --index-url https://download.pytorch.org/whl/cu128
.\venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env          # add FREELLMAPI_API_KEY (and Spotify keys if you use them)
copy sugar.example.yaml sugar.yaml   # optional
```

Models download on first run (Whisper into `D:\Sugar_Models\Whisper`, MeloTTS into the Hugging Face cache).

## Running

```powershell
.\venv\Scripts\python.exe main.py                 # voice + UI window
.\venv\Scripts\python.exe main.py --window edge   # UI in an Edge app window
.\venv\Scripts\python.exe main.py --no-voice      # type only
.\venv\Scripts\python.exe main.py --text "what time is it"   # one-shot, prints the reply
.\venv\Scripts\python.exe main.py --debug         # verbose logs + developer panel
```

In the window: `` ` `` opens the developer panel (latency per turn, health, live events, memory),
`Ctrl+M` toggles the microphone, `Esc` stops Sugar.

## Things to say

| You say | What happens |
|---|---|
| "Sugar." | "Yeah?" — it's listening |
| "Open VS Code." / "Close Spotify." | app launched or closed, no model involved |
| "Play Master of Puppets by Metallica." / "Next song." | Spotify, media keys as fallback |
| "Volume 40." / "What's the weather?" | instant |
| "Open my JIVA project." | project activated, Claude Code connected |
| "Analyze the project and tell me what's broken." | Claude Code in read-only plan mode |
| "Fix all three." / "Run the tests." | Claude Code works in the background, you hear progress |
| "What is Claude doing?" / "Pause the coding session." / "Continue." | session control |
| "Show me what changed." | git diff summary spoken, full diff on screen |
| "Don't commit yet." / "Commit the changes." | commits only when asked |
| "Remember that I prefer Python." / "What do you remember about me?" | long-term memory |
| "Wait." / "Stop." / "Never mind." | interrupt, cancel |

## Project layout

```text
sugar/
  app/           application wiring and lifecycle
  audio/         capture, VAD, endpointing, echo guard, STT, TTS, playback, streaming speech
  intelligence/  conversation manager, fast path, router, context, working memory, response processing
  providers/     FreeLLMAPI (OpenAI-compatible), Ollama, Claude (API / CLI), fallback pool
  agent/         agent loop, permissions, tool executor
  tools/         desktop, web, weather, media, filesystem, terminal, memory, coding, calculator
  coding/        project registry, Claude Code CLI runner, coding sessions
  memory/        SQLite + FTS5 store
  ui/            WebSocket bridge, window host, web UI
  config/        layered settings
  core/          events, state machine, metrics, logging, processes
melo/            vendored MeloTTS (English)
tests/           offline unit and integration tests
main.py          entry point
```

Runtime data (database, logs, sessions, metrics, screenshots) lives in `data/` and is not committed.
See [ARCHITECTURE_AUDIT.md](ARCHITECTURE_AUDIT.md) for the audit of the previous version and the design
rationale.

## Configuration

Settings are layered: defaults → `sugar.yaml` → `data/overrides.json` (changes made in the UI) →
environment variables `SUGAR__SECTION__KEY`. Secrets are read only from the environment / `.env`.

## Tests

```powershell
.\venv\Scripts\python.exe -m pytest                         # offline suite
$env:SUGAR_MODEL_TESTS=1; .\venv\Scripts\python.exe -m pytest tests\test_stt.py tests\test_tts.py   # real models
```

## Measured latency (RTX 5050 laptop)

| Stage | Time |
|---|---|
| Fast-path command, text to first audio | ~0.2 s |
| Whisper final transcript (large-v3-turbo, GPU) | ~0.26 s, usually overlapped with the end-of-turn pause |
| Partial transcript (small.en, GPU) | ~0.09 s |
| MeloTTS first sentence (GPU) | ~0.1 s |
| FreeLLMAPI first token | 1.2–2.5 s (varies by routed backend) |
| Speaker output latency (WASAPI) | ~23 ms |

## Known limitations

- No acoustic echo cancellation is available to PortAudio on Windows; Sugar uses an echo guard
  (learned speaker-to-mic coupling + transcript matching). A headset gives the best barge-in.
- Ollama and VS Code are unavailable while the drive that stores them is disconnected; Sugar reports
  this and keeps working with the other providers.
- FreeLLMAPI latency depends on which free backend it routes to.
