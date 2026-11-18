# SID — Provider Routing

Status labels as in `SID_UNIVERSAL_AI_ARCHITECTURE.md`.

## Providers

| Name | What it is | Capabilities (`routing.CAPABILITIES`) | Status |
|---|---|---|---|
| `freellm` | FreeLLMAPI, OpenAI-compatible, online | chat, reasoning, creative, coding, tools, fast, external | VERIFIED |
| `claude` | Claude via the API (key) or the Claude Code CLI | chat, reasoning, creative, coding, strong, external | IMPLEMENTED (in chains; not the first choice in the live runs) |
| `ollama` | local Ollama 0.40.16, `qwen2.5:3b`, binary and models on `D:\Sugar_Models\ollama` | chat, coding, tools, local, offline | VERIFIED |

Claude Code (the engineering engine) is not a chat provider and is never in these chains; only
`project_analysis` and `build_execution` reach it.

## Routes per category

Defaults (`routing.DEFAULT_ROUTES`), overridable in `sid.yaml` under `llm.routes`:

| Category | Chain |
|---|---|
| general_conversation, roleplay, productivity, image_generation | freellm → ollama → claude |
| general_knowledge, coding_qa, creative_writing, education, health_information | freellm → claude → ollama |
| math_reasoning | claude → freellm → ollama |
| research, document_creation, desktop | freellm → ollama (tool-capable only) |

`chain_for(category, need_tools, local_only)`:

1. Start from the category's preferred order.
2. Keep providers that have the required capabilities: `tools` when the category offers tools,
   `local` when the request carries the user's own health data.
3. Drop providers whose circuit is open (recent failures). If every circuit is open, still try the
   capable ones rather than go mute.
4. **Latency-aware:** if the first provider's time-to-first-token EWMA is more than 3× the second's,
   with at least 3 samples each, swap them.

The chosen chain and the reason are published on `route.selected` and shown in telemetry.

## Fallback and health

- Each provider has a circuit: a failure opens it for 10 s, doubling per consecutive failure up to
  300 s; a success closes it. A provider that fails **before** producing text is skipped and the next
  one answers the same request; one that fails mid-answer keeps what was said and reports the error.
- A health loop checks every provider at start and every 60 s and publishes `providers.status`
  (availability, failures, last error, TTFT, local flag).
- Connectivity state for the UI and replies:

| State | Meaning |
|---|---|
| online | every configured provider healthy |
| degraded | some providers down, an online one still works |
| local_fallback | online providers configured but all down; the local model answers |
| local_only | no online provider configured (offline mode); the local model answers |
| offline | nothing answers; SID says so instead of hanging |

## Managed local server — VERIFIED

`sugar/providers/ollama_server.py`: when Ollama is enabled and not already running, SID starts the
portable `D:\Sugar_Models\ollama\ollama.exe serve` with `OLLAMA_MODELS=D:\Sugar_Models\ollama\models`,
waits for `/api/version`, and stops its own process tree on shutdown (an Ollama that was already
running is left alone). Live: status `started` on start, `stopped` after shutdown, no `ollama.exe`
left behind.

## Measured (live, 18 Nov 2026)

| Run | Provider | Result |
|---|---|---|
| Black holes, poem, roleplay, closure, haemoglobin (online) | freellm | all answered; first token ~2.5 s |
| "My haemoglobin is 9.8 g/dL. Is that low?" | ollama only (`local_only`) | correct, recommended a clinician |
| Online providers disabled (`SID__LLM__FREELLM__ENABLED=false`, `SID__LLM__CLAUDE__ENABLED=false`) | ollama | black holes in 2.6 s total, 4-line poem in 0.8 s; first token 2266 ms cold, 304 ms warm |
| Unit: online provider fails health check | ollama | answered, the failing provider not waited on, state `local_fallback` |

## Limits

- `qwen2.5:3b` is small: good for short answers and offline use, weaker on long reasoning. A larger
  model fits on D: but needs more than the 8 GB of VRAM shared with Whisper and MeloTTS.
- FreeLLMAPI is a free service; its latency varies and it has no SLA.
- Capability tags are static; they are not measured.
