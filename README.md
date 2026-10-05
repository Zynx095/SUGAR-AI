# Sugar

A voice-first assistant for Windows that operates your computer. Sugar listens continuously, answers
while it is still thinking, and stops the moment you talk over it. It types into apps, manages windows,
drives your own browser and plays the exact video you asked for, then checks that each action worked.
Real software work goes to **Claude Code**, so you can build things hands-free.

```text
mic → Silero VAD → echo guard → endpointer ─┐            ┌→ fast-path grammar ─┐
                                            ├→ Whisper ──┤                     ├→ tools → computer control
         speaker ← MeloTTS ← speech chunker ┘            └→ FreeLLMAPI / Ollama / Claude (agent) ─┘   (verified)
                                                          └→ Claude Code sessions (real CLI)
```

## What it does

- **Continuous listening** with a wake word ("Sugar") and a follow-up window, so a conversation doesn't
  need the wake word on every turn.
- **Natural turn-taking**: a pre-roll buffer keeps your first syllable, and the pause Sugar waits before
  answering depends on what you said ("open Chrome" ends fast, "I want you to…" waits).
- **Streaming end to end**: model tokens become sentences, and sentences become audio while the rest is
  still being generated. Code, tables, paths and stack traces are shown on screen, not read aloud.
- **Interruptions**: talk over Sugar and it ducks, then stops within one audio block. "Wait" gets a
  "Yeah?", and "stop" cancels whatever is running (or pauses the music if Sugar was idle).
- **Computer control**:
  - **Typing**: real keystrokes into the app you're using, in real time or pasted for long text. Sugar
    checks the text actually arrived.
  - **Keys and shortcuts**: any shortcut, plus editing commands like "delete the last line" and "save".
  - **Windows and apps**: open, switch to, minimize, maximize, restore, snap and close them. When an app
    asks whether to save, Sugar asks you.
  - **Browser tabs**: new tab, close "this tab" or "the YouTube tab", switch, back, forward, reload,
    and search any site.
  - **Media**: Spotify, or YouTube resolved by real search and ranking, then verified on the page.
  - **Desktop context**: Sugar knows what's in front, so "this", "that" and "the app I just opened"
    work.
- **Instant commands** without a model, including compound ones: "open Notepad and type hello", "stop
  the music and close Spotify".
- **Model routing**: FreeLLMAPI for conversation, Ollama as the offline fallback, and Claude for deep
  reasoning (API key or your Claude subscription through the CLI). Requests that operate the computer
  get the full computer toolset.
- **Claude Code integration**:
  - Open a project by name or spoken path ("the project in D colon College JIVA").
  - Delegate analysis or fixes, and hear progress and a spoken summary.
  - Pause and resume across restarts, and approve Claude Code's requests by voice.
  - Commits only happen when you ask for one.
- **Safety model**: read, non-destructive, sensitive and destructive levels, decided by what an action
  actually does. Risky actions get a spoken confirmation. File access is confined to allowed folders,
  and deletes go to the Recycle Bin.
- **Memory**: conversation history plus explicit long-term memories ("remember that I prefer Python").
  They're searchable, listable and deletable.

## Requirements

- Windows 10 (1809+) or 11, Python 3.12
- An NVIDIA GPU is strongly recommended (tested on an RTX 5050 laptop GPU). CPU works with smaller
  Whisper models and slower speech.
- [FreeLLMAPI](https://freellmapi.co/) desktop app running (the default chat provider)
- Optional: [Ollama](https://ollama.com/) for offline answers, [Claude Code](https://claude.com/claude-code)
  for coding sessions, Spotify developer credentials, a YouTube Data API key

## Setup

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install torch==2.7.1 --index-url https://download.pytorch.org/whl/cu128
.\venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env          # add FREELLMAPI_API_KEY (and Spotify / YouTube keys if you use them)
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

In the window, `` ` `` opens the developer panel (latency per turn, health, live events, memory),
`Ctrl+M` toggles the microphone, and `Esc` stops Sugar.

## Things to say

| You say | What happens |
|---|---|
| "Sugar." | "Yeah?": it's listening |
| "Open Notepad." | Notepad opens or comes to the front. If it's already open you get a fresh tab, so your notes stay untouched |
| "Type: Hello, this is Sugar AI." | typed into Notepad key by key, then read back |
| "Press enter and type: This is a hands-free computer." | two steps, in order |
| "Delete the last line." / "Save the file." / "Select all." / "Undo that." | editing in the app in front |
| "Type this Python code." | types the code block from Sugar's last answer |
| "Close this." / "Close Notepad." | closes what's in front (in a browser: the tab). Unsaved work → "Should I save them, not save, or cancel?" |
| "Open Chrome." | opens Chrome, or your default browser if Chrome isn't installed, and says so |
| "Open a new tab." / "Close this tab." / "Close the YouTube tab." / "Reopen the last tab." | browser tabs |
| "Go back." / "Go forward." / "Reload the page." / "Scroll down." | browser navigation |
| "Search YouTube for Blinding Lights." / "Search GitHub for FastAPI WebSockets." | results in the browser |
| "Open the most relevant result." / "Play the second video." | picks from the last search |
| "Play Blinding Lights by The Weeknd on YouTube." | searches, ranks, opens the official video and checks the page title |
| "Play the latest MrBeast video." / "Open the video about RTX 5050 benchmarks." | the newest full upload from that channel / the best match |
| "Play the video in my clipboard." | opens that exact link |
| "Pause." / "Resume." / "Skip." / "What's playing?" | whatever is actually playing: Spotify, YouTube or any player |
| "Switch to VS Code." / "Minimize Discord." / "Bring Chrome back." / "Snap Notepad to the left." | windows |
| "What's open?" / "Force close Discord." | window list / end an app that won't close |
| "Volume 40." / "What's the weather?" | instant |
| "Open my JIVA project." → "Analyze the project." → "Fix the biggest issue." | Claude Code on the repo, with progress spoken |
| "What is Claude doing?" / "Pause the coding session." / "Show me what changed." | session control |
| "Remember that I prefer Python." / "Wait." / "Stop." | memory, interruptions |

## How computer control works

```text
utterance ─→ FastPath grammar (desktop view: what "this" is) ─→ tool call ──┐
         └─→ agent model (computer tools + desktop context) ─→ tool calls ──┤
                                                                           ▼
                  ToolRegistry → PermissionManager → ToolExecutor (origin: user | model)
                                                                           ▼
   ComputerControl — one sugar-desktop worker thread (COM MTA, per-monitor DPI aware)
     WindowManager · KeyboardController · ApplicationController · BrowserController
     MediaController · ScreenController · DesktopContext (WinEvent foreground hook)
                                                                           ▼
   ComputerActionResult(success, action, target, details, verified, error)
```

Each action uses the lowest-level mechanism that works:

1. **Win32 API** (ctypes): windows, focus, show state, close, geometry, input, clipboard, processes.
2. **UI Automation** (comtypes): browser tabs and the address bar, editor text for read-back, dialog
   buttons, elements to click by name.
3. **Keyboard and mouse injection** (SendInput): typing, shortcuts and pointer actions, always into a
   window that's verified to be in front.
4. **Vision**: the `VisionProvider` interface exists, but no model is connected yet, so Sugar says it
   can't see rather than guessing.

**Typing.** Characters go through the target window's keyboard layout as real key presses. Unicode
packets are used only for characters the layout can't produce. Every key event is sent on its own with a
short gap, because Windows 11 Notepad garbles batched input. Before each character Sugar checks the
window still has focus, so typing stops rather than spilling into another app. Real-time mode runs
around 15–20 ms per character. Auto mode pastes text over 300 characters, and multi-line code into
editors that auto-indent. A paste restores your clipboard afterwards, every format included. Sugar never
types into its own window: "type …" goes to the app you were last using.

**Windows aren't processes.** "Close Notepad" sends a polite close to Notepad's *windows*, so the app
can still ask to save, and Sugar relays that question. "Force close" ends processes, and only when you
say so. "Close this" in a browser closes the tab, and in a Notepad with several tabs it closes the
document in front.

**Browser.** Sugar drives the browser on your screen: it finds installed browsers and the default in
the registry, reads tabs and the address bar through UI Automation, and closes tabs with their own Close
button. Addresses are typed, never pasted, so your clipboard stays untouched. Searches reuse a blank
tab.

**YouTube.** The request is parsed into title, artist or creator, "latest", "official" and audio vs
video. Search uses the YouTube Data API when `YOUTUBE_API_KEY` is set, otherwise the public results
page. Candidates are ranked on title and artist match, official channels and uploads, covers and
lyrics penalties, duration, recency and position. The chosen video opens in your browser (reusing the
YouTube tab), and the page is then checked against the request: id in the address bar, title,
relevance. A mismatch moves on to the next candidate, and playback is confirmed through Windows media
sessions. Starting YouTube pauses Spotify (and vice versa), configurable with `computer.pause_other_media`.

**No invented links.** The Rickroll bug came from the model writing YouTube URLs itself. A model may
now only open a YouTube ID that Sugar has seen in a search result, the clipboard or your own words.
Otherwise the tool refuses and points it to `media.play`.

**Permissions.** These follow from what an action does:

| Level | Examples |
|---|---|
| read | window list, desktop context, now playing, page text, screenshots |
| non-destructive | typing, shortcuts, opening/switching/closing windows and tabs (apps still ask to save), navigation, media |
| sensitive (asks when a model proposes it) | force close, "don't save", clear everything, Win-key and Ctrl+Alt shortcuts, raw clicks, clicking "Buy"/"Delete"/"Send" by name, Delete in File Explorer |
| destructive (always asks) | Shift+Delete, deleting files |

## Project layout

```text
sugar/
  app/           application wiring and lifecycle
  audio/         capture, VAD, endpointing, echo guard, STT, TTS, playback, streaming speech
  computer/      computer control: windows, keyboard, apps, browser, media/YouTube, screen, desktop context
  intelligence/  conversation manager, fast-path + computer grammar, router, context, working memory
  providers/     FreeLLMAPI (OpenAI-compatible), Ollama, Claude (API / CLI), fallback pool
  agent/         agent loop, permissions, tool executor
  tools/         computer, browser, media, web, weather, filesystem, terminal, memory, coding, calculator
  coding/        project registry, Claude Code CLI runner, coding sessions
  memory/        SQLite + FTS5 store
  ui/            WebSocket bridge, window host, web UI
  config/        layered settings
  core/          events, state machine, metrics, logging, processes
melo/            vendored MeloTTS (English)
tests/           unit and integration tests (in-memory desktop), opt-in live desktop tests
benchmarks/      latency benchmarks (voice, computer control, browser, media resolution)
main.py          entry point
```

Runtime data (database, logs, sessions, metrics, screenshots, benchmark results) lives in `data/` and is
not committed. [ARCHITECTURE_AUDIT.md](ARCHITECTURE_AUDIT.md) has the audits behind the design, and
[BENCHMARK_REPORT.md](BENCHMARK_REPORT.md) has the measurements.

## Configuration

Settings are layered: defaults → `sugar.yaml` → `data/overrides.json` (changes made in the UI) →
environment variables `SUGAR__SECTION__KEY`. Secrets are read only from the environment / `.env`. The
`computer:` section covers:

- typing mode and pace
- paste threshold and clipboard restore
- preferred browser
- music platform
- pausing other media
- YouTube region

See `sugar.example.yaml`.

## Tests

```powershell
.\venv\Scripts\python.exe -m pytest                         # offline suite (in-memory desktop, no models)
$env:SUGAR_MODEL_TESTS=1; .\venv\Scripts\python.exe -m pytest tests\test_stt.py tests\test_tts.py   # real models
$env:SUGAR_DESKTOP_TESTS=1; .\venv\Scripts\python.exe -m pytest tests\test_desktop_live.py         # real desktop
```

The live desktop tests act only on windows they create: a Tk text window, a temporary file in its own
Notepad tab, and a separate browser window. They skip when a game or full-screen app is in front and give
the foreground back afterwards.

Benchmarks: `python benchmarks\voice_latency.py`, `computer_control.py`, `browser_control.py`, and
`media_resolution.py [--live]`. Results go to `data\benchmarks\`, and the report is in
[BENCHMARK_REPORT.md](BENCHMARK_REPORT.md).

## Known limitations

- No acoustic echo cancellation is available to PortAudio on Windows. Sugar uses an echo guard instead
  (learned speaker-to-mic coupling plus transcript matching). A headset gives the best barge-in.
- Windows blocks injected input into apps running as administrator. Sugar detects this and says so,
  instead of typing into nothing.
- Without `YOUTUBE_API_KEY`, YouTube search reads the public results page. It works without a key, but
  YouTube can change that page.
- Browser control covers Chromium browsers (Brave, Chrome, Edge, Opera, Vivaldi) fully. Firefox gets
  the keyboard-level actions.
- There is no vision model, so pixel-level questions ("compare this UI with the previous version") are
  answered honestly as unsupported.
- Ollama and VS Code are unavailable while the drive that stores them is disconnected. Sugar reports this
  and keeps working with the other providers.
- FreeLLMAPI latency depends on which free backend it routes to.
