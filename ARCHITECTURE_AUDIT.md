# Sugar AI — Architecture Audit (Phase 0)

Audit date: 2026-10-05. Branch audited: `main` @ `7170f59` plus the uncommitted
working tree (snapshotted as `c6fbeb3` on `rework/sugar-v2`).

Everything below was verified against the code and the machine, not the README
(which still describes the upstream "Jarvis MLX" macOS project).

---

## 1. What actually exists

| Path | Status | Notes |
|---|---|---|
| `main.py` (649 lines) | **Crashes on import** | Monolith: audio loop, wake word, commands, Spotify, LLM call, TTS, and the CustomTkinter UI in one file. |
| `brain_models.py` | Used | Ollama router + single non-streaming `chat()` call. |
| `config.py` | Used | Constants only. Missing names that `main.py` imports. |
| `stt/VoiceActivityDetection.py` | Used | PyAudio + WebRTC VAD, 10 ms frames, 2.5 s end-of-speech timer. |
| `stt/whisper/*` (12 files + assets) | **Dead** | Apple **MLX** Whisper port. `mlx` cannot run on Windows; nothing imports it. |
| `melo/*` | Used | Vendored MeloTTS (English only). Imports `cached_path` (→ boto3/botocore/google-cloud-storage), `torchaudio`, `librosa` that inference never uses. |
| `tools/calculator.py` | **Broken** | Not a calculator: it is a stale copy of an old `brain_models.py`. `main.py` imports `extract_arithmetic`/`calculate` from it, gets `ImportError`, and silently falls back to stubs returning `None`. Arithmetic never worked. |
| `tools.py` | **Dead** | Shadowed by the `tools/` package (packages win over same-named modules), so it is unreachable. Contains `eval()`. |
| `memory.py` + `chroma_db/` | **Dead** | ChromaDB vector memory. Not imported by current `main.py`. The previous version stored `"user -> reply"` pairs but explicitly disabled retrieval ("BLANK SLATE MEMORY"). 86 stale documents. `chroma_db/chroma.sqlite3` is committed to git. |
| `test/test_llm.py`, `test/test_tts.py` | **Dead** | `test_llm` needs `mlx_lm` (macOS only). No real tests exist. |
| `practice_project/` (+ 40 MB `.venv`) | **Unrelated** | Empty Python packaging template ("Start coding in Python today!"). `.vscode/` launch + settings point at it instead of Sugar. |
| `beep.mp3` | **Dead** | Only reference was commented out. |
| `prompt.txt` | **Dead** | Persona text never loaded by any code. |
| `__pycache__/models.cpython-312.pyc` | Stale | Bytecode for a deleted `models.py`. |
| `venv/` (5.5 GB) | Runtime env | Created for `jarvis-mlx-main` at another path (its `Scripts/*.exe` shims point there; `python -m pip` works). Contains chromadb, kubernetes, grpc, opentelemetry, boto3, botocore, google-cloud-storage that nothing needs. |
| `.env` | Secrets | `SPOTIFY_CLIENT_ID/SECRET` only. Gitignored. |
| `.cache` | Secret | Spotify OAuth token cache. Gitignored. |
| `Sugar_Chat_Logs.txt` | User data | Append-only chat log. Gitignored. |

## 2. Current architecture (as implemented)

```text
PyAudio mic (16 kHz, 10 ms frames, default device, opened at import time)
  └─ WebRTC VAD mode 3 → buffer until 2.5 s of continuous non-speech
       └─ queue → _transcription_loop (polls every 20 ms)
            └─ faster-whisper large-v3-turbo on **CPU int8** (blocking)
                 └─ substring wake word ("sugar" / "wake up" / "so go")
                      └─ substring command matching (os / spotify / typing)
                           └─ Ollama router call (gemma3:1b, JSON)   ← extra LLM round trip
                                └─ Ollama chat, **non-streaming**, full history window
                                     └─ regex cleanup → MeloTTS on **CPU**, whole reply
                                          └─ sd.play(); sd.wait()   ← blocks the worker
```

The UI thread polls an event queue every 50 ms. All inference is serialized.
There is no barge-in: the microphone pipeline keeps buffering while Sugar
talks, and the buffer is flushed afterwards.

## 3. Defects found

### 3.1 Startup / crash bugs (current working tree)
1. `main.py:40` imports `DEFAULT_MASTER_PROMPT` from `config`, which does not
   define it → **`ImportError`, Sugar cannot start at all.**
2. `main.py:522` uses `DEFAULT_ROUTE` (never imported) → `NameError` when the
   "ready" event arrives.
3. `main.py:559` uses `ACCENT_AMBER` (never defined) → `NameError` kills the
   status pulse animation the first time Sugar is "PROCESSING".
4. `main.py:44` arithmetic import always fails (see `tools/calculator.py`).

### 3.2 Environment facts that break the current design
5. **Ollama is down.** `OLLAMA_MODELS=E:\UserBenchmark\OllamaModels` and the
   `E:` drive is not present, so `ollama serve` exits immediately
   (`mkdir E:\UserBenchmark: The system cannot find the path specified`).
   Because routing *and* answering both go through Ollama, Sugar has no
   intelligence at all in this state. `ollama list` (run by `main.py` at
   startup) also launches the Ollama tray app, which then crash-loops.
6. **VS Code** resolves to `E:\UserBenchmark\Microsoft VS Code\Code.exe`
   (same missing drive) and `code` is not on `PATH`, so "open VS Code"
   (`subprocess.Popen(["code"], shell=True)`) silently does nothing.
7. **GPU unused.** The GPU is an RTX 5050 Laptop (Blackwell, sm_120). The
   venv's `torch 2.6.0+cu124` has no sm_120 kernels (`no kernel image is
   available for execution on the device`), so MeloTTS cannot use it, and
   CTranslate2 borrows that build's CUDA 12.4 cuBLAS (int8 GEMMs fail with
   `CUBLAS_STATUS_NOT_SUPPORTED`). Whisper is hard-coded to CPU anyway.
8. **FreeLLMAPI is installed and healthy but unused.** It is the FreeLLMAPI
   desktop app (v0.9.8) serving an OpenAI-compatible API on
   `http://127.0.0.1:31415/v1` (port from `%APPDATA%\FreeLLMAPI\config.json`).
   Auth is `Authorization: Bearer $FREELLMAPI_API_KEY` (user env var,
   `freellmapi-…`). Model ids: `auto`, `auto:<fast|smart|reliable|balanced|cheap>`,
   `auto:<profile>`, `fusion`, plus concrete ids. Streaming is standard
   `chat.completion.chunk` SSE terminated by `data: [DONE]`; tool calling
   works (Gemini tool calls carry a `thought_signature` that must be echoed
   back). Routed model is reported in the `X-Routed-Via` header. The
   `claude-*` ids it lists are **aliases mapped to other free models**, not
   Claude — they must never be presented as Claude.
9. **Claude Code CLI 2.1.289** is installed (`~/.local/bin/claude.exe`), using
   subscription auth (`apiKeySource: none`). Verified in this audit:
   `-p --output-format stream-json --verbose` emits `system/init`
   (session_id, tools, model, permissionMode), `assistant` (thinking / text /
   tool_use blocks), `user` (tool results), `rate_limit_event`, and `result`
   (`is_error`, `result`, `num_turns`, `total_cost_usd`,
   `permission_denials`). `--resume <id>` continues a session (verified),
   `--include-partial-messages` adds `stream_event` deltas,
   `--permission-mode` accepts `acceptEdits|auto|bypassPermissions|manual|dontAsk|plan`.
   The CLI waits 3 s for stdin unless stdin is closed. No `ANTHROPIC_API_KEY`
   is configured.

### 3.3 Voice pipeline problems (the root of "robotic")
10. **2.5 s fixed end-of-speech timer** (`VoiceActivityDetection.py:26`) —
    every turn waits 2.5 s of silence before anything happens, yet natural
    mid-sentence pauses longer than that still cut the user off.
11. **Speech onset is clipped.** Speech starts only after two consecutive
    voiced 10 ms frames and there is no pre-roll buffer. The chat log shows
    the consequence repeatedly: "*Lee* Master of Puppets", "*lame slow*
    puppets", "*Lay* love story by Taylor Swift".
12. **Utterances longer than 30 s lose their beginning** (`deque(maxlen=3000)`).
13. **Whisper on CPU**: measured 6.2–14.2 s to transcribe a 6.7 s clip.
14. **Language is not pinned** → Japanese, Russian, German transcripts of
    English speech/noise in the log; classic Whisper silence hallucinations
    ("Thank you.", "Продолжение следует…") reach the LLM.
15. **2.5 s of trailing silence is sent to Whisper** with every utterance,
    which encourages exactly those hallucinations.
16. `sensitivity` is accepted by `VADDetector` and never used; the UI slider
    does nothing.
17. **No streaming anywhere**: router call → full non-streaming generation →
    full TTS synthesis → playback. MeloTTS CPU RTF measured at 0.6–0.9
    (cold first call 3.4 s for "Okay.").
18. **No barge-in.** `speak()` blocks on `sd.wait()`; the mic buffer is
    discarded afterwards.
19. **Echo**: nothing prevents Sugar hearing itself; it is only avoided
    because listening is disabled while speaking.
20. `force_sleep()` runs `speak()` on the Tk main thread → UI freezes for the
    whole synthesis + playback.

Estimated end-to-end latency of the old pipeline before the first audible
word: 2.5 s (endpoint) + ~6 s (CPU STT) + ~0.5 s (router LLM) + several
seconds (full generation) + ~0.7× reply duration (full TTS) ≈ **10–20 s**.

### 3.4 Intent handling problems
21. Substring matching causes destructive false positives:
    - `"type" in text or "write" in text` → "write me a function" or
      "prototype" **types text into whatever window has focus**.
    - `"time" in text and "what" in text` → "what time complexity…" answers
      with the clock.
    - `"pause" in text` → "pause the coding session" pauses Spotify.
22. The LLM claims actions it cannot perform ("Playing Master of Puppets on
    Spotify for you!") — there is no tool layer, so the model can only talk.
23. The committed version supported Spotify *play <song>*, *previous*, and
    *resume*; the working tree dropped them (regression).
24. Every non-command request pays for an extra router LLM call.
25. Concurrent requests are silently dropped (`processing_lock.acquire(blocking=False)`).

### 3.5 Security
26. `tools.py` uses `eval()` (unreachable today, but a trap).
27. Typing commands inject arbitrary transcribed text into the focused
    window with no confirmation.
28. No permission model, no path policy, no audit trail.
29. `chroma_db/chroma.sqlite3` (conversation content) is committed to git.
30. No secret leakage found in tracked files; `.env` and `.cache` are ignored.

## 4. Latency measurements on this machine

| Stage | Measured |
|---|---|
| Whisper large-v3-turbo, CPU int8, 6.7 s clip | 6.2–14.2 s |
| Whisper large-v3-turbo, CUDA fp16 (cu124 cuBLAS) | 0.70 s (fixed 30 s window cost) |
| Whisper small.en, CUDA fp16, 2 s clip | 0.15 s |
| Whisper base.en, CUDA fp16, 2 s clip | 0.055 s (lower accuracy) |
| Silero VAD v6 (bundled with faster-whisper), per 32 ms window | 0.12 ms |
| MeloTTS CPU, "I found the problem." | 0.8–0.95 s (RTF ≈ 0.65) |
| MeloTTS CPU, cold first call | 3.4 s |
| FreeLLMAPI `auto` time-to-first-byte | 1.1–1.5 s (occasionally ~4 s) |
| Claude Code CLI round trip (haiku, trivial prompt) | ~4.7 s (≈3.5 s CLI start-up) |

## 5. Dependencies

Needed: numpy, sounddevice, faster-whisper (ctranslate2, onnxruntime, av,
tokenizers), torch (MeloTTS), transformers (MeloTTS BERT), g2p_en, inflect,
huggingface_hub, soundfile, pydantic, pydantic-settings, PyYAML,
python-dotenv, httpx, spotipy, pyautogui, pyperclip.

Not needed after the rework: PyAudio and webrtcvad (replaced by
sounddevice + Silero), chromadb (+ its kubernetes/grpc/opentelemetry tree),
cached_path (+ boto3/botocore/s3transfer/google-cloud-storage), torchaudio,
librosa (only referenced by unused MeloTTS training helpers), ollama
(replaced by a direct streaming HTTP client), customtkinter (UI replaced),
playsound, mlx/mlx-lm/tiktoken/numba (MLX Whisper).

## 6. Risks

- A voice agent that can run commands is dangerous if intents are matched by
  substring (see 3.4) or if model output can trigger tools without policy.
- Repository contents and web pages are untrusted input that will be fed to
  models; they must never be able to raise Sugar's permissions.
- Free providers are rate-limited and slow at times; Sugar must degrade
  (FreeLLMAPI → Ollama → Claude CLI) and say so instead of going silent.
- Two GPU workloads (Whisper + MeloTTS) share an 8 GB laptop GPU.
- External drive `E:` holds Ollama models and VS Code; both vanish with it.

## 7. Proposed architecture

```text
 Mic (sounddevice, 16 kHz, 32 ms blocks, auto-reconnect)
   └─ AudioFrontend thread: Silero VAD → EchoGuard → Endpointer (pre-roll, adaptive pause)
        ├─ speech_started / paused / resumed / ended  ──────────────┐
        └─ barge-in candidate while Sugar speaks → duck → verify → stop
                                                                    ▼
 asyncio core ─ EventBus ─ StateMachine (IDLE/LISTENING/TRANSCRIBING/THINKING/
   │                                       TOOL_EXECUTION/SPEAKING/INTERRUPTED/ERROR/PAUSED)
   ├─ STT worker: small.en (partials, wake word, turn hints) + large-v3-turbo (final)
   ├─ ConversationManager: engagement/wake word, hallucination filter, turn cancellation
   ├─ Router: FastPath grammar → control / tool / coding / agent / chat / reasoning
   ├─ Orchestrator
   │    ├─ Tools (registry → PermissionManager → Executor; voice confirmation)
   │    ├─ Chat: FreeLLMAPI stream (fallback Ollama → Claude) with escalation tools
   │    ├─ Agent loop: plan → tool → observe → verify → answer
   │    └─ Coding: ProjectRegistry + Claude Code sessions (real CLI, stream-json)
   ├─ Context: recent turns + working memory + relevant memories + project/tool state
   ├─ Memory: SQLite + FTS5 (preferences, project notes, sessions, history)
   └─ ResponseProcessor: markdown/code/path-aware SpeechChunker + normalizer
        └─ TTS worker (MeloTTS, GPU when available; SAPI fallback) → AudioPlayer (flushable)
 UI: local WebSocket bridge (token-protected) → web UI in a native window
```

Key decisions:
- **asyncio core + dedicated threads** only where libraries block (PortAudio
  callbacks, CTranslate2, torch). No component blocks the event loop.
- **Silero VAD** (already bundled) replaces WebRTC VAD; adaptive endpointing
  uses transcript completeness instead of a fixed 2.5 s timer.
- **Two-tier STT** on GPU: small.en for partials/wake word/turn hints,
  large-v3-turbo for the final transcript, started speculatively when the
  user pauses so it overlaps the endpoint wait.
- **Streaming end to end**: tokens → sentence chunker → TTS → playback queue.
- **Barge-in** with duck-then-verify so echo cannot interrupt Sugar.
- **FreeLLMAPI is the default chat provider**; Ollama is the offline fallback;
  Claude (API key if present, otherwise the Claude CLI with tools disabled) is
  the reasoning provider. Claude Code (agentic) is driven only through the
  real CLI.
- **No substring commands.** A token-boundary grammar with slot extraction
  handles fast commands; everything ambiguous goes to a model that can only
  act through permission-checked tools.
- **Web UI** (HTML/CSS/canvas) over a local, token-authenticated WebSocket,
  hosted in a native window (pywebview or Edge app mode). Tk cannot do the
  smooth, layered, state-driven visuals required, and the event-stream
  boundary keeps the UI fully decoupled from the core.

## 8. Cleanup plan (verified references)

Remove: `stt/whisper/` (MLX), `stt/VoiceActivityDetection.py` (replaced),
`brain_models.py`, `config.py`, `memory.py`, `tools.py`, `tools/` (broken
copy), `test/` (MLX tests), `practice_project/`, `beep.mp3`, stale
`__pycache__/`. Export the 86 Chroma documents to `data/legacy/` before
removing `chroma_db/` and untracking it. Fold `prompt.txt`'s style guidance
into the persona, then remove it. Repoint `.vscode/` at Sugar. Move the
legacy chat log into `data/legacy/`.

---

# Part 2 — Computer-control audit (V2.1)

Audit date: 2026-10-06, branch `rework/sugar-v2` @ `d735377`. Evidence comes from
the code, from `data/logs/sugar.jsonl` (the user's own voice session), and from
probes run on this machine (Windows 11 26200, Brave as default browser, Store
Notepad 11.2607).

## 9. What V2 had for computer control

| Piece | Where | What it really did |
|---|---|---|
| `keyboard.type` | `tools/desktop.py` | Waited a fixed 1.5 s, copied the text to the clipboard with pyperclip, pressed Ctrl+V with pyautogui. No target window, no focus, no check that anything arrived. Restored only the clipboard's text. Group `agent` only. |
| `keyboard.press` | `tools/desktop.py` | `pyautogui.hotkey` over a fixed allow-list of 60 key names; always SENSITIVE (asks first when proposed by a model). |
| `app.open` | `tools/desktop.py` + `tools/apps.py` | Start-menu catalog (`Get-StartApps`) and `shell:AppsFolder` launch. Fire and forget: no window wait, no focus, no "already running" check. |
| `app.close` | same | `taskkill /IM <image>`, a process-level close of every window of that image name, with no verification. Group `agent` only. |
| `browser.open_url` / `web.search` | `tools/web.py` | `webbrowser.open` (default browser, always a new tab). Accepts any URL the model writes. |
| `media.*` | `tools/media.py` | Spotify Web API only; pyautogui media keys as fallback. No YouTube. |
| `screen.capture`, `clipboard.*` | `tools/desktop.py` | pyautogui screenshot, pyperclip text clipboard. |
| Window management, tabs, UI Automation, focus tracking, desktop context | — | **None.** Nothing in V2 enumerated windows or knew what was in the foreground. |

## 10. Why the reported failures happen

**10.1 Real-time typing.** Four separate causes, all visible in the user's log:

1. Grammar: the fast path only accepted utterances starting with
   `type / type out / type in / write down / dictate`. "Write directly into
   the notebook." and "…and start writing a Fibonacci code" went to the router.
2. Routing: those requests were classified `chat` (the `_AGENT` regex looks
   for files/terminal words), and the chat toolset (`CHAT_TOOLS`) does not
   contain `keyboard.type`. The model had no way to type. Log:
   `Open a new notepad and start writing a Fibonacci code…` produced only
   `app.open(Notepad)`; `write directly into the notebook.` produced no tool call.
3. No target: even when `keyboard.type` ran, it pasted into whatever had
   focus, which while talking to Sugar is often Sugar's own window, after a
   blind 1.5 s sleep.
4. The OS layer is harder than it looks. Measured on this machine, the
   Windows 11 Notepad editor (`RichEditD2DPT`) garbles injected input that
   arrives in a batch:
   `KEYEVENTF_UNICODE` batches came out as `"Hello gggar\riiis tttttest…"`,
   and batched `Shift + key` lost the Shift (`"Hello sugar"`, `")"` → `"0"`)
   because the app reads the *current* modifier state when it processes the
   message. Sending each event on its own with a 4 ms gap was exact in
   every run (113-character sample with symbols and newlines).

**10.2 Closing applications and windows.** `app.close` exists but (a) is not in
the chat toolset, (b) is reachable from the fast path only when the name
resolves in the Start-menu catalog ("brave browser" did not), (c) works on
process image names with `taskkill`, so a process and a window are the same
thing to it, and (d) has nothing to resolve "this", "that" or "it" against.
Log: `Now close all the tabs and close Brave` → no tool;
`Stop playing music on Spotify and close Spotify.` → no tool (compound commands
were not understood by the grammar and the chat model did nothing).

**10.3 Browser tabs.** There is no browser subsystem: no idea which browsers
are installed (Chrome is **not** installed here; Brave is the default), no tab
list, no back/forward/reload/close-tab actions, no verification. Every page
opened with `webbrowser.open` became a new tab in the default browser.

**10.4 YouTube → Rick Astley.** There is no hard-coded URL anywhere in the
repository (searched for `Rick`, `Astley`, `dQw4w9WgXcQ`, `youtube.com/watch`,
`youtu.be`; the only hits are the CMU pronouncing dictionary and the
`youtube.com` home page in the site list). The log shows the cause:

```text
"Be brave, search for YouTube and open any video of your choice."
  route chat → model gpt-oss-120b → browser.open_url(https://www.youtube.com/watch?v=dQw4w9WgXcQ)
"Open another YouTube video of your choice on Brave"
  route chat → browser.open_url(https://www.youtube.com/watch?v=dQw4w9WgXcQ)   (again)
"Let's open up a song by a Hindi artist."
  → browser.open_url(https://www.youtube.com/watch?v=J8a3c6ZVb8w)              (invented ID)
```

The model had no way to *search* YouTube, `browser.open_url` accepted any URL
it wrote, and nothing checked what opened. Language models reproduce the most
memorised video ID on the internet when asked to make one up. Two more gaps
fed it: `media.play` was Spotify-only and its slot filter rejected any query
containing "video", and "on YouTube" was not parsed at all.

**10.5 Installed automation libraries** (venv, before this pass):
`comtypes 1.4.17` (pulled in by pycaw, so UI Automation is already
available), `psutil 7.2.2`, `pyautogui 0.9.54` (+ PyGetWindow, PyScreeze,
pymsgbox, pytweening, MouseInfo), `pyperclip`, `pillow`. Not installed:
pywin32, pywinauto, uiautomation, pynput, Playwright, Selenium, any WinRT
projection.

**10.6 Dependencies that are genuinely needed.**

| Need | Choice | Why |
|---|---|---|
| Windows, focus, input, clipboard, processes | `ctypes` (stdlib) | Win32 `EnumWindows`, `SetForegroundWindow`, `SendInput`, `ShowWindow`, `PostMessage(WM_CLOSE)`, clipboard API. pywin32/pywinauto would add nothing these calls don't already do. |
| Tabs, address bar, dialogs, page elements | UI Automation through `comtypes` (already installed) | Probe: Brave exposes every tab as a `TabItem` with a selection state and an invokable **Close** button, and the omnibox as an `Edit` with a `ValuePattern` (12–52 ms per query). Notepad exposes its tabs (with "Modified"/"Unmodified") and its document text through `TextPattern`. |
| What is playing, targeted pause/resume | `winrt-Windows.Media.Control` (+ runtime, Foundation, Collections; ~0.5 MB) | Windows' own media-session API (SMTC). Lists Spotify, browsers and other players with title, artist and playing state, and controls one session without touching the others. Probe: 34 ms to list sessions. |
| Process lookup | `psutil` (already installed, now declared) | Window → process name; force-close process trees. |

Not added: **Playwright/Selenium** drive a separate automation browser and
profile, not the Brave window the user is looking at; Chromium ignores remote
debugging on the default profile since version 136, so attaching to the
user's own browser over CDP is not an option. UI Automation reaches the real
window instead. pyautogui is replaced by the `SendInput` engine and is no
longer used.

## 11. V2.1 design

```text
voice → STT → FastPath grammar (deterministic, uses live desktop context)
              │  "close this tab", "open notepad and type …", "play X on YouTube"
              └→ Router → agent model (computer toolset + desktop context in the prompt)
                         │
                         ▼
               ToolRegistry → PermissionManager → ToolExecutor
                         │
                         ▼
         ComputerControl (one "sugar-desktop" worker thread, COM MTA, DPI aware)
           ├─ WindowManager       EnumWindows, focus (with fallbacks), min/max/restore, WM_CLOSE, snap
           ├─ KeyboardController  layout-aware SendInput, paced events, realtime / paste, hotkeys
           ├─ Clipboard           Win32 clipboard, full snapshot + restore around pastes
           ├─ ApplicationController Start-menu catalog + windows: open/focus/close/force-close
           ├─ BrowserController   installed/default browser, tabs (UIA), navigation, search engines
           ├─ MediaController     SMTC sessions + Spotify API + YouTube (search → rank → open → verify)
           ├─ ScreenController    capture, UIA inspect, click/scroll/drag; VisionProvider interface
           └─ DesktopContext      foreground tracker (WinEvent hook), "this/that/it", action history
                         │
                         ▼
               ComputerActionResult(success, action, target, details, verified, error)
```

Control levels, lowest that works wins: (1) Win32 API, (2) UI Automation,
(3) keyboard/mouse injection, (4) vision (interface only; no model is wired
in, so nothing pretends to "see").
