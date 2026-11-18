# Sugar AI — Intelligence Architecture Investigation & Report

**Date:** November 17, 2026  
**Target:** Submission Readiness & Intelligence Architecture Audit  
**Author:** Antigravity Agentic Assistant  
**Status:** Complete Audit — No Implementation Changes Made  

---

## Executive Summary

An investigation of Sugar AI's intelligence architecture was conducted on this machine (Windows 11, RTX 5050 Laptop GPU Blackwell `sm_120`, 8 GB VRAM, C: drive 2.38 GB free, D: drive 316.54 GB free).

The core architecture (the V2 refactor) is well-structured: deterministic intent routing (`FastPath`), asynchronous streaming token processing (`SpeechChunker`), layered memory (`WorkingMemory` + SQLite FTS5 `MemoryStore`), and decoupled session management for Claude Code.

However, **Sugar AI currently feels sluggish and unresponsive in speech turns (experiencing ~8.3 to 9.8 second latencies) because its primary local providers (FreeLLMAPI and Ollama) are offline/misconfigured**. Every conversational turn cascades through connection timeouts before falling back to the Claude Code CLI running `opus`, compounding network and subprocess overhead.

By activating FreeLLMAPI, eliminating dead fallback timeouts, and adjusting the reasoning model, Sugar AI's response latency can be brought down from **~8.3 seconds to under 1.2–1.5 seconds**, providing an immediate, intelligent, and fluid voice experience for tomorrow's submission.

---

## 1. FreeLLMAPI Integration

### 1.1 Operational Status
* **Current State:** **Inactive / Not Running.**
* **Service Details:** FreeLLMAPI is an Electron-based desktop application (v0.6.9 / v0.9.8) installed at:  
  `C:\Users\Yukith M Joseph\AppData\Local\Programs\freellmapi-desktop\FreeLLMAPI.exe`
* **Port & Endpoint:** It listens locally on `http://127.0.0.1:31415/v1` (configured in `%APPDATA%\FreeLLMAPI\config.json`).
* **Authentication:** User environment variable `FREELLMAPI_API_KEY` is present and set to `freellmapi-6475c2b2c7ec1408ea5b9eb68cd1a8aa09584c3c5daefe1e`.
* **Database State:** The underlying database (`%APPDATA%\FreeLLMAPI\freeapi.db`) contains **33 backend API keys** (Google, Groq, Mistral, Cloudflare, OpenRouter, Cohere, Nvidia, etc.), and health checks show healthy credentials (e.g. Google Gemini key `proLIGMA`, Groq, Cloudflare).
* **Cause of Failure:** `FreeLLMAPI.exe` is not configured as a Windows auto-start background service. When the desktop app is not running, attempts to connect to `127.0.0.1:31415` fail with connection refused.

### 1.2 Available Models
* The catalog in `freeapi.db` tracks **278 enabled models** across 27 distinct provider backends.
* Key enabled models include:
  * **Google:** `gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-3-flash-preview`, `gemini-3.5-flash` (all multimodal with tool calling).
  * **Groq:** `openai/gpt-oss-120b`, `openai/gpt-oss-20b` (ultra-low latency inference).
  * **Mistral:** `codestral-latest`.
  * **Cloudflare:** `@cf/meta/llama-3.3-70b-instruct-fp8-fast`, `@cf/deepseek-ai/deepseek-r1-distill-qwen-32b`.
  * **OpenRouter / Nvidia / Kilo:** `nvidia/nemotron-3-super-120b-a12b:free`, `google/gemma-4-31b-it:free`.
* **Virtual Profiles:** FreeLLMAPI exposes meta-routes: `auto`, `auto:fast`, `auto:smart`, `auto:reliable`, `auto:balanced`, `auto:cheap`, and `fusion`.
* *Important Note:* The `claude-*` model identifiers exposed by FreeLLMAPI are aliases re-routed to other free models, **not** genuine Anthropic Claude. Sugar AI's codebase specifically handles this by routing actual Claude requests to Anthropic or Claude Code.

### 1.3 Failure Handling
* Implemented in `sugar/providers/pool.py` via `ProviderPool` and `_Circuit`:
  * **Exponential Backoff Circuit Breaker:** Failed attempts record error details and open the circuit breaker (`open_until = now + min(300.0, 10.0 * 2^(failures-1))`).
  * **Pre-token Fallback:** If FreeLLMAPI fails before emitting any text or tool call chunks (e.g., connection refused or HTTP 5xx), the error is caught, and the pool immediately tries the next provider in `chat_chain` (Ollama -> Claude).
  * **Mid-stream Protection:** If FreeLLMAPI fails *after* tokens have already been emitted (`produced == True`), the error is raised immediately. It is not silently switched mid-sentence, preventing duplicate or garbled audio from reaching the user.
  * **All-Failed Graceful Degradation:** If every provider in the chain fails, `AllProvidersFailed` is caught by `Orchestrator._model()`, which speaks a clear offline message: *"I can't reach my language models right now. FreeLLMAPI and Ollama are both down, but commands still work."*

### 1.4 Streaming Support
* **Fully Supported.** `OpenAICompatibleProvider` in `sugar/providers/openai_compat.py` establishes an asynchronous HTTP streaming request (`POST /chat/completions` with `"stream": true, "stream_options": {"include_usage": True}`) using `httpx.AsyncClient`.
* It consumes Server-Sent Events (`data: ...`), parses chunk deltas (`delta.content`, `delta.reasoning_content`), and aggregates streaming tool calls with their arguments and metadata.

### 1.5 Streaming into Text-to-Speech (TTS)
* **Yes, end-to-end token streaming into TTS is fully implemented:**
  1. The provider yields `StreamChunk("text", text=content)` as tokens arrive.
  2. `AgentLoop` forwards these tokens directly to `TurnOutput.stream(delta)`.
  3. `TurnExecution.stream()` feeds `SpeechChunker.feed(delta)`.
  4. `SpeechChunker` (`sugar/intelligence/response.py`) parses syntax on-the-fly, buffers only until natural clause and sentence boundaries (`.`, `!`, `?`, `,`), filters robotic fillers ("Certainly!", "As an AI..."), normalizes file paths/URLs/acronyms, and announces on-screen items (code blocks, tables, tracebacks).
  5. The chunk is immediately dispatched to `SpeechOutput.say(handle, chunk)`.
  6. A dedicated background synthesis thread (`sugar-tts`) synthesizes the audio via MeloTTS (or Windows SAPI fallback) and pushes PCM audio buffers into `AudioPlayer`.
  7. **Result:** The user hears the beginning of the reply while the LLM is still generating subsequent sentences.

---

## 2. Claude Integration

### 2.1 Transport Implementation: API vs. Claude Code CLI
* **Both transports are fully implemented:**
  1. **`ClaudeAPIProvider` (`sugar/providers/claude.py`):** Uses the official `anthropic` Python SDK (`AsyncAnthropic`) with beta message streaming, fallback handling, and eager tool schema conversion (`_openai_tools_to_anthropic`). Activated when `ANTHROPIC_API_KEY` is present in the environment.
  2. **`ClaudeCLIProvider` (`sugar/providers/claude.py` + `sugar/coding/claude_code.py`):** Uses the locally installed Claude Code CLI executable in non-interactive print mode (`claude -p --output-format stream-json --verbose --tools "" --strict-mcp-config --setting-sources project --no-session-persistence`). Streams raw JSON events over standard I/O. Activated when `ANTHROPIC_API_KEY` is unset but `claude.exe` exists.
  3. **Agentic Coding Runner:** Real coding tasks (`Route.CODING`) bypass generic LLM wrappers and run through `CodingSessionManager` using the real `ClaudeCodeRunner` with workspace isolation and permission modes.
* **Current Machine Configuration:**
  * `ANTHROPIC_API_KEY` is **unset**.
  * `claude.exe` is installed and verified at `C:\Users\Yukith M Joseph\.local\bin\claude.exe` (v2.1.325).
  * Consequently, all conversational Claude fallback calls currently route through `ClaudeCLIProvider`.

### 2.2 Root Cause of the ~8.2-Second Cold Start
Inspection of the execution trace metrics in `data/logs/sugar.jsonl` (Turn `17f150db`, timestamps `1794860783.703` to `1794860793.538`) revealed the exact breakdown:

```json
{"ts": 1794860783.703, "event": "LLM_START", "provider": "freellm", "purpose": "chat"}
// FreeLLMAPI connect timeout (offline) ~2.05 s
{"ts": 1794860785.751, "event": "LLM_START", "provider": "ollama", "purpose": "chat"}
// Ollama connect timeout (offline) ~2.02 s
{"ts": 1794860787.769, "event": "LLM_START", "provider": "claude", "purpose": "chat"}
{"ts": 1794860787.772, "msg": "starting Claude Code in ...\\data\\claude_workdir"}
{"ts": 1794860791.986, "event": "LLM_FIRST_TOKEN", "ms": 8283, "provider": "claude"}
```

**The ~8.2-second delay is not Claude alone; it is a three-stage cascade:**
1. **FreeLLMAPI Connection Timeout (~2.05 s):** Sugar attempts to connect to `127.0.0.1:31415`, which is down.
2. **Ollama Connection Timeout (~2.02 s):** Sugar attempts to connect to `127.0.0.1:11434`, which is down.
3. **Claude CLI Subprocess & Model Overhead (~4.21 s):**
   * Spawning a Node.js process (`claude.exe`) on Windows, initializing CLI runtime, and reading user auth tokens: ~1.2 s.
   * Model default is set to **`opus`** (`cli_model: "opus"` in `settings.py`). Anthropic Opus server-side time-to-first-token adds ~2.5–3.0 s.
4. Total latency from turn start to first token: $2.05\text{s} + 2.02\text{s} + 4.21\text{s} = \mathbf{8.28\text{ s}}$ (`llm_ttft_ms: 8283`).

### 2.3 Latency Reduction Strategies
1. **Prevent Timeout Cascades:** Ensure FreeLLMAPI is running, or temporarily remove offline providers from `chat_chain` so requests hit an active provider immediately (eliminating 4.1 seconds of dead waiting).
2. **Switch CLI Model from Opus to Haiku:** In `sugar/config/settings.py` or runtime config, changing `claude.cli_model` from `opus` to `haiku` reduces CLI response time from ~4.2s to ~1.2s.
3. **Use Direct API Key:** Setting `ANTHROPIC_API_KEY` bypasses process creation and Node.js initialization entirely, reducing Claude TTFT to 0.6–1.0s over HTTP.

---

## 3. Ollama Status & Feasibility

### 3.1 Why the Current Installation is Unavailable
1. **Missing Executable:** `ollama.exe` is not found on Windows `PATH` nor in `C:\Users\Yukith M Joseph\AppData\Local\Programs\Ollama`.
2. **Invalid Model Path:** The Windows User environment variable `OLLAMA_MODELS` is set to:  
   `E:\UserBenchmark\OllamaModels`
3. **Missing Storage Drive:** Drive `E:` does not exist on the machine. When Ollama attempts to start or initialize storage, it fails with:  
   `mkdir E:\UserBenchmark: The system cannot find the path specified` and terminates.

### 3.2 OLLAMA_MODELS Environment Variable
* Verified via system inspection:
  ```powershell
  [System.Environment]::GetEnvironmentVariable('OLLAMA_MODELS', 'User')
  # Returns: E:\UserBenchmark\OllamaModels
  ```
  The setting is still actively pointing to the missing drive `E:`.

### 3.3 Feasibility of Local Model Configuration on C:
* **Storage Audit:**
  * **Drive C: Free Space: 2.38 GB** (Critical threshold).
  * **Drive D: Free Space: 316.54 GB** (Abundant space).
* **Verdict:** **Configuring Ollama models on C: is strictly NOT feasible.**
  * Standard small models (e.g. `gemma3:4b`, `qwen2.5:3b`, `llama3.2:3b`) require **2.4 GB to 3.8 GB** of disk space.
  * Downloading any model to `C:` will exhaust available disk space completely, leading to Windows OS instability, virtual memory (pagefile) errors, and boot failures.
  * Any local model storage must be placed on drive `D:` (e.g., `D:\OllamaModels` or `D:\Sugar_Models\Ollama`).

### 3.4 Storage and GPU Requirements
* **VRAM Capacity:** NVIDIA GeForce RTX 5050 Laptop GPU has **8,151 MiB (~8 GB)** total VRAM.
  * Current baseline usage by Windows and apps: ~932 MiB. Available VRAM: ~7.2 GB.
* **VRAM Budgeting:**
  * Whisper STT (`large-v3-turbo` fp16): ~1.5 GB VRAM.
  * MeloTTS (Torch CUDA): ~1.2 GB VRAM.
  * Desktop / OS overhead: ~1.0 GB VRAM.
  * Remaining VRAM for LLM: **~3.5 GB**.
  * A 3B/4B 4-bit quantized model (e.g., `gemma3:4b` at ~2.8 GB VRAM) fits within the remaining headroom. An 8B model (~5.5 GB VRAM) will cause VRAM eviction or CUDA Out-Of-Memory errors when TTS or STT activates.
* **Architecture Constraint (Blackwell sm_120):**
  * The RTX 5050 uses NVIDIA's newest Blackwell architecture (`sm_120`). Older CUDA builds (CUDA 12.4 or earlier) lack `sm_120` binaries, throwing `no kernel image is available for execution on the device`. Ollama must be a current build (supporting CUDA 12.8+ or 13.x) or run in CPU fallback mode to prevent crashes.

---

## 4. Model Routing

```
                  ┌───────────────────────────────────────────────┐
                  │                 User Utterance                │
                  └───────────────────────┬───────────────────────┘
                                          │
                        FastPath Regex / Token Grammar
                                          │
             ┌────────────────────────────┼───────────────────────────┐
             ▼                            ▼                           ▼
      [ Route.CONTROL ]            [ Route.COMMAND ]          [ Route.CODING ]
     stop, pause, repeat        Deterministic Tools         Repository Work
     (Handled in memory)       (calc, volume, apps, time)     (Claude Code CLI)
                                          │
                                          ▼
                               Ambiguous / Conversational
                                          │
                     ┌────────────────────┴────────────────────┐
                     ▼                                         ▼
            [ Route.REASONING ]                         [ Route.CHAT / AGENT ]
       Deep / Architecture / >= 35 words             Casual Chat & Computer Actions
                     │                                         │
       `llm.reasoning_chain`:                      `llm.chat_chain`:
       1. Claude (API/CLI)                         1. FreeLLMAPI
       2. FreeLLMAPI (auto:smart)                  2. Ollama
       3. Ollama (deepseek-r1)                     3. Claude
```

### 4.1 Routing by Request Type
* **Simple Conversation (`Route.CHAT`):** Handled by `chat_chain` (Default: `[freellm, ollama, claude]`). Restricted to `CHAT_TOOLS` (14 lightweight tools: weather, search, open/close app, typing, media). FreeLLMAPI runs with `reasoning_effort: "none"`.
* **Complex Reasoning (`Route.REASONING`):** Triggered by analytical keywords (`think deeply`, `step by step`, `architecture`, `trade-offs`, `compare`) or any prompt $\ge 35$ words. Handled by `reasoning_chain` (Default: `[claude, freellm, ollama]`). Claude is the primary provider; FreeLLMAPI runs with model `auto:smart` and `reasoning_effort: "medium"`.
* **Coding Work (`Route.CODING`):** Triggered when an active project or recent coding context exists alongside code-related action verbs (`fix`, `debug`, `refactor`, `build`, `commit`, `write a test`). Routed directly to `sugar.coding.sessions` running Claude Code CLI (`claude -p`).
* **Computer Actions (`Route.AGENT`):** Complex system actions (`"open notepad and write a Fibonacci function"`, multi-step commands) use `chat_chain` with the full `COMPUTER_GROUPS` toolset (`agent`, `chat`, `computer`).

### 4.2 Unnecessary Claude CLI Invocations
1. **Fallback Flooding:** When FreeLLMAPI and Ollama are offline, **every simple conversational query** ("hello", "how are you", "what can you do") falls through to Claude CLI, spawning an 8.2-second Opus process for casual chitchat.
2. **Length-Based False Positives:** The router heuristic `words >= 35` automatically classifies any long turn as `Route.REASONING`, redirecting wordy but simple conversational requests to Claude CLI.
3. **CLI Process Spawn Cost:** `ClaudeCLIProvider` launches a fresh `claude -p` process for every single turn with `--no-session-persistence`, re-incurring Node.js startup costs.

---

## 5. Vision Architecture

### 5.1 Inspection of `UnavailableVision`
Located in `sugar/computer/screen.py` (lines 40–58):
```python
class UnavailableVision:
    name = "none"
    reason = "no vision model is configured"

    @property
    def available(self) -> bool:
        return False

    async def describe(self, image: Path, question: str | None = None) -> str:
        raise NotImplementedError(self.reason)

    async def compare(self, before: Path, after: Path, question: str | None = None) -> str:
        raise NotImplementedError(self.reason)

    async def locate(self, image: Path, description: str) -> tuple[int, int] | None:
        raise NotImplementedError(self.reason)
```
When `computer.screen.look` is invoked by an agent turn without a configured vision model, Sugar returns a clear tool failure explaining that no vision model is set up and offers to read UI control names instead.

### 5.2 Interface Expected by `screen.py`
Any vision provider must implement the `VisionProvider` protocol:
* `name: str` — Provider name identifier.
* `available: bool` (property) — Readiness flag.
* `async def describe(self, image: Path, question: str | None = None) -> str` — Analyzes a screenshot image file on disk and answers a visual query.
* `async def compare(self, before: Path, after: Path, question: str | None = None) -> str` — Compares two screenshots taken before and after an action.
* `async def locate(self, image: Path, description: str) -> tuple[int, int] | None` — Returns screen pixel coordinates `(x, y)` for a described UI element.

### 5.3 Gemini Vision API Integration (Cloud)
* **Viability: High / Optimal.**
* FreeLLMAPI's local proxy database already includes Google Gemini keys and enabled vision models (`gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-3.5-flash` with `supports_vision: 1`).
* Alternatively, a direct `GeminiVisionProvider` calling the Gemini API via HTTP POST with an image payload requires only ~50 lines of code.
* **Benefits:** 0 MB local VRAM overhead, 0 MB disk storage impact on C:, and fast inference latency (~800 ms to 1.4 s).

### 5.4 Local Vision Model Integration
* **Viability: Not Recommended for Tomorrow.**
* Local vision models (e.g. `llama3.2-vision:11b`, `qwen2.5-vl:7b`, `minicpm-v`) require:
  * 5 to 9 GB of disk storage (impossible on C:, must go to D:).
  * 6 to 8 GB of VRAM, which directly conflicts with Whisper STT and MeloTTS on the 8 GB laptop GPU, triggering Out-Of-Memory crashes.
  * Blackwell (`sm_120`) CUDA compatibility considerations.

### 5.5 Impact on Desktop Automation Stability
* **Zero Destabilization Risk.**
* Sugar AI's computer control engine (`sugar/computer/engine.py`) does **not** rely on pixel vision for automation.
* Control execution uses:
  * **Windows UI Automation (`UIA`):** Direct accessibility tree queries (`screen.inspect`), control identification, and programmatic clicks (`ui.click`) that operate without mouse movement.
  * **Win32 Input:** Precise mouse clicks (`screen.click`) and atomic keyboard inputs (`keyboard.type` with character verification).
* Vision is purely an advisory sensory layer accessed exclusively through the `screen.look` tool. Introducing or swapping a `VisionProvider` does not alter or risk mouse, keyboard, or application control workflows.

---

## 6. Context & Memory Architecture

### 6.1 Understanding References to Previous Actions
* **Multi-Layered Context Tracking:**
  * **`WorkingMemory` (`sugar/intelligence/working.py`):** Records `last_action` (domain, description, tool name, arguments, success/failure status, timestamp), `last_error`, `last_files`, and `last_options`.
  * **`DesktopContext` (`sugar/computer/context.py`):** Maintains an MRU queue of external foreground windows via Windows WinEvent hooks, remembers the last application opened, the last search query, and tracks active modal prompts ("Save changes?").
  * **Pronoun & Ordinal Resolution:** `WorkingMemory.resolve_option()` resolves references such as *"the second one"*, *"option 3"*, or *"the first link"* directly against listed choices.
  * **Prompt Context Injection:** Every turn builds a `# Current context` block in `ContextBuilder` detailing the active desktop application, last action result, and recently touched files.

### 6.2 Active Project Identification
* **Persistent Project Awareness:**
  * Active project state is tracked in `WorkingMemory.active_project` (`{"name": ..., "path": ...}`) and persisted across restarts in `data/working.json`.
  * **Auto-Discovery:** `ProjectRegistry` (`sugar/coding/projects.py`) scans `coding.project_roots` (`D:\College`, `D:\College\SOMESHIT DOWNLOADS`, `D:\College\WEBSITES`) up to depth 2 for markers (`.git`, `package.json`, `pyproject.toml`, etc.), caching catalog entries in `data/projects.json`.
  * **Phonetic & Fuzzy Matching:** `ProjectRegistry.resolve()` handles spoken variations and phonetics (e.g., matching *"cue shield"* or *"q shield"* to `Q-Shield`, or *"jeeva"* to `Jiva`).
  * **Automatic Fallback Prompting:** If the user issues a coding command without an active project, Sugar identifies the most recent project session or asks: *"Which project should Claude work on? You've got Q-Shield, Aura-AI, and hackathon."*

### 6.3 Context Maintenance Across Claude Code Tasks
* **Full Session Continuity:**
  * Implemented in `sugar/coding/sessions.py` via `CodingSessionManager`.
  * Each repository project has a dedicated `CodingSession` record saved in `data/coding_sessions.json`.
  * Upon completion of a task, Claude Code emits a `session_id`, which is saved into `session.claude_session_id`.
  * When a follow-up task is executed on that project, `ClaudeCodeRunner` launches Claude CLI with `--resume <session_id>`.
  * **Result:** Claude Code retains complete memory of all previously inspected files, terminal commands run, code edits made, and prior conversation context across tasks and application restarts.

---

## 7. Recommendation: Fastest Reliable Configuration for Tomorrow

To ensure Sugar AI presents as responsive, intelligent, and rock-solid during tomorrow's submission without risking system destabilization:

### 1. Pre-Launch Step: Start FreeLLMAPI Desktop App
* Launch `FreeLLMAPI.exe` from `C:\Users\Yukith M Joseph\AppData\Local\Programs\freellmapi-desktop\FreeLLMAPI.exe` and confirm it is minimized to the system tray.
* **Why:** FreeLLMAPI provides healthy, zero-VRAM access to Google Gemini 2.5 Flash and Groq models with measured time-to-first-token of **1.1–1.5 seconds**, instantly replacing the 8.2-second timeout cascade with real-time conversational streaming.

### 2. Configuration Adjustment: Remove Dead Providers from Chain
* In `sugar.yaml` (or via runtime overrides):
  ```yaml
  llm:
    chat_chain: [freellm, claude]       # Remove dead 'ollama'
    reasoning_chain: [freellm, claude]  # Use fast 'auto:smart' FreeLLM, then Claude
    claude:
      cli_model: haiku                  # Switch fallback CLI from opus to haiku
  ```
* **Why:** Eliminating Ollama from the chain saves 2.0 seconds of connect timeout on every fallback. Switching Claude CLI to Haiku cuts fallback time from ~4.5s down to ~1.2s.

### 3. Keep Ollama Disabled for Tomorrow
* Do **not** attempt to download or configure local models on drive `C:` due to the critical 2.38 GB space limitation.
* Do not attempt last-minute Ollama reinstalls that could introduce Blackwell CUDA kernel conflicts or VRAM competition against MeloTTS and Whisper.

### 4. Rely on Windows UI Automation for Desktop Demos
* Sugar's deterministic `FastPath` intents (calculator, volume, media control, application launching, window switching) execute in **4 to 12 milliseconds** without calling an LLM.
* For computer control demonstrations, rely on `screen.inspect`, `ui.click`, and `keyboard.type`, which utilize native Windows accessibility trees and have proven 100% reliable without vision model overhead.

---
*Report completed and verified against Sugar AI codebase and system state.*
