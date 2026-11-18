# SID — Universal AI Architecture

Branch `sid/transform`, checkpoint tag `sid-checkpoint-before-universal` (the engineering-only SID).
Status labels used throughout: **VERIFIED** (exercised on this machine with real models / real Claude
Code / real browser), **IMPLEMENTED** (code + automated tests, not exercised live), **PARTIALLY
IMPLEMENTED**, **NOT IMPLEMENTED**, **BLOCKED**.

## What changed

SID was an engineering-only assistant that refused everything else. It is now a universal assistant
that still keeps Claude Code strictly for engineering work. The change is architectural, not a prompt
edit: a category classifier picks a handler, a tool allow-list and a provider route for every request,
and a security layer decides every tool call from where the request came from.

```text
voice ─ VAD ─ endpointer ─ Whisper ─ wake check (exact / verified look-alike) ─┐
typed text ───────────────────────────────────────────────────────────────────┤
                                                                              ▼
                       Router  (sugar/intelligence/router.py)
   1 self-commands (voice, personality, roleplay end, health consent)  ── deterministic, no model
   2 grammar commands (stop, volume, open app, media, project select)  ── deterministic, no model
   3 engineering classifier (domain.classify)  ── CODING_QA / PROJECT_ANALYSIS / BUILD_EXECUTION
   4 universal categories (categories.classify_universal)  ── 20 categories, ordered detectors
                                                                              ▼
                       Orchestrator  (sugar/intelligence/orchestrator.py)
   ANSWER ─ category instructions + category tools ─ AgentLoop ─ ProviderRouter chain ─┐
   CODING_QA ─ model, no tools                                                         │
   PROJECT_ANALYSIS / BUILD_EXECUTION ─ Claude Code CLI in the explicit project        │
   DESKTOP ─ desktop agent (computer tools, max 10 steps) ─────────────────────────────┤
   COMMAND ─ tool call, no model                                                       │
                                                                                       ▼
   every tool call ─ PermissionManager.authorize(tool, args, provenance) ─ ALLOW / CONFIRM / DENY
                                                                                       ▼
   streamed text ─ SpeechChunker (first phrase early) ─ MeloTTS ─ OpenVoice / DSP ─ speaker
   events ─ EventBus ─ UI (entity state + palette, Workbench, telemetry) + TelemetryStore
```

## Categories

`sugar/intelligence/categories.py`. Each category has a `CategorySpec`: instructions added to the
system prompt, the tools the model may use (empty = none), a provider route, an entity mode and a step
limit. Detectors run in order; the engineering classifier still runs first so code work never falls into
a chat path, and ordinary conversation never reaches Claude Code.

| Category | Handled by | Tools offered to the model | Status |
|---|---|---|---|
| general_conversation | model | none | VERIFIED |
| general_knowledge | model | none | VERIFIED ("Explain how black holes work") |
| coding_qa | model | none | VERIFIED ("What is a closure in Python?") |
| project_analysis | Claude Code, `plan` mode (read-only) | Claude Code's own | IMPLEMENTED (tests with fake CLI) |
| build_execution | Claude Code in the selected project | Claude Code's own | VERIFIED (created `reports/` in a test project, $0.17) |
| creative_writing | model, entity mode `creative` | none | VERIFIED (dark fantasy poem) |
| roleplay | model, own context layer `roleplay:<id>` | none | VERIFIED (astronaut/alien, ended, no leak) |
| education | model, tutoring instructions | none | IMPLEMENTED |
| math_reasoning | model (reasoning route) | `calc.evaluate` | IMPLEMENTED |
| research | model, entity mode `research` | `web.lookup`, `web.fetch` (public hosts only) | IMPLEMENTED |
| health_information | model with medical safety instructions; emergencies answered without a model | none | VERIFIED |
| productivity | model | memory tools | IMPLEMENTED |
| desktop_control | grammar or desktop agent | computer tools | IMPLEMENTED (live desktop suite not re-run, see acceptance report) |
| browser_control | grammar or desktop agent | browser tools | IMPLEMENTED |
| media_control | grammar (YouTube/Spotify search, playback) | media tools | IMPLEMENTED |
| file_operations | desktop agent | filesystem tools inside `permissions.allowed_roots` | IMPLEMENTED |
| document_creation | model | `filesystem.write`, `filesystem.list`, `folder.open` | IMPLEMENTED |
| image_generation | model writes a prompt; no image model is connected | none | NOT IMPLEMENTED (honest fallback) |
| system_configuration | grammar (volume, brightness…) or agent; privileged commands always confirm | computer tools | IMPLEMENTED |
| ambiguous | SID asks; nothing runs | none | VERIFIED ("Build a Python application…" with no project asks for one) |

Adding a category: add a `Category` value, a `CategorySpec`, a detector in `DETECTORS`, and (optionally)
a provider route in `llm.routes`. Nothing else needs to change.

## Context layers

| Layer | Where | Behaviour |
|---|---|---|
| Persona + personality | `context.py` `PERSONA`, `personality.py` | SID's identity plus one of sid / cinematic / friendly / professional / creative; independent of the voice |
| Category instructions | `CategorySpec.instructions` | added for that turn only |
| Conversation history | `memory/store.py` turns with `meta.context` | roleplay turns are tagged `roleplay:<id>`; normal turns never see them and the scene never sees normal turns (VERIFIED live) |
| Working memory | `working.py` | active project (never restored as current on start), previous project, roleplay state |
| Untrusted content | `agent/loop.py` | every tool output is redacted and wrapped as `[UNTRUSTED CONTENT from <tool>…]` |

## Engineering and project isolation (unchanged guarantees, one fix)

- Claude Code runs only in a project selected this session, named in the request, or typed as a path
  inside `permissions.allowed_roots`. No project → SID asks (VERIFIED).
- **Fixed in this pass:** "Modify my portfolio" was answered as general knowledge. The verb's object is now
  resolved against known projects, so a named project wins over an unrelated active repository; when the
  request names only the project ("modify my portfolio"), SID selects it and asks what to change instead
  of starting Claude Code without a task (VERIFIED live: selected `yukith-portfolio` while `SUGAR-AI`
  was active).

## Context-aware "stop"

Priority, implemented in `ConversationManager._control` (`conversation.py`) and covered by tests:

1. SID is speaking → speech stops within one audio block. IMPLEMENTED (barge-in tests)
2. A turn is running (model answer or desktop action) → the turn is cancelled; a step already handed to
   Windows finishes, nothing after it runs. IMPLEMENTED
3. Only Claude Code is running → SID asks "stop the Claude Code task in <project>?"; yes stops the
   process, no leaves it running. IMPLEMENTED (`test_stop_while_only_claude_code_is_running_asks_first`)
4. Nothing of SID's is active → "stop" pauses the media that is playing (`on_idle_stop`). IMPLEMENTED

## Personalities and voices

Independent settings: `conversation.personality` changes how SID writes; `tts.voice_profile` changes how
it sounds and drives the entity palette. "be more professional", "switch to your female voice", "make
your voice deeper", "switch back to your original voice" are parsed locally (`self_commands.py`) and run
as tools (`app/self_tools.py`), persisted in `data/overrides.json`. VERIFIED live. See
`SID_VOICE_SYSTEM.md`.

## Telemetry

`sugar/core/telemetry.py` stores measured latencies, provider failures, Claude Code task durations and
costs, test/build outcomes, voice changes, wake activations and rejections, tool outcomes and
non-allow security verdicts in `data/telemetry.db`. It never stores conversation text, transcripts or tool
arguments (tested). Retention `telemetry.retention_days` (30). UI: Telemetry → History (date range,
daily-median chart, provider comparison, errors, CSV/JSON export). IMPLEMENTED; History tab VERIFIED in
headless Edge.

## Internal rename (`sugar` → `sid`)

**NOT IMPLEMENTED, deliberately.** The spec allows it only once everything else is stable, and a package
rename touches every import and the user's data. User-facing naming is SID everywhere; `SID__` env vars
and `sid.yaml` already work, with `SUGAR__` and `sugar.yaml` still read. Migration plan:

1. Add a `sid` package that re-exports `sugar` (`sid/__init__.py` with a module alias), switch entry
   points and tests to `sid`, keep `sugar` as the shim for one release.
2. Move code with `git mv sugar sid` in one commit, leave `sugar/__init__.py` as an import shim that
   warns.
3. Data: `data/sugar.db` → `data/sid.db` on first start (copy, keep the original, record the migration
   in the settings overrides); `sugar.yaml` keeps being read.
4. Remove the shim and the legacy names after a release with no reports.

## Not done / limits

- The category classifier is rule-based (tested on several hundred phrasings across suites). Unusual
  phrasing can land in a neighbouring category; it fails toward answering or asking, never toward
  executing.
- No image generation model.
- The free online provider (FreeLLMAPI) takes 2–3 s to the first token; the local model answered short
  questions in 0.3–2.3 s (cold start included) but is a 3B model and weaker on hard questions.
