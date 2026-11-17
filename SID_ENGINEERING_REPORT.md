# SID — engineering report

Branch `sid/transform` (from `rework/sugar-v2` at `f44fe80`). Nothing was pushed or merged.

## Starting point

- Working tree had uncommitted user work (README, `main.py`, four reports, `benchmarks/`, two live test
  files). It was committed unchanged as the checkpoint `d50508d` before any SID change.
- Baseline suite (excluding `tests/test_desktop_live.py`, which drives the real desktop):
  **347 passed, 2 skipped** — the reported baseline held.
- Frontend: vanilla ES modules served by SID's own token-protected server, a 2D canvas orb, no WebGL,
  no bundler, CSP `default-src 'self'` (so Three.js had to be vendored, not loaded from a CDN).
- Q-Shield came from `data/working.json`: the last active project was restored on every start.

## Commits

| Commit | Phase |
|---|---|
| `d50508d` | checkpoint of the existing working tree |
| `f38b28d` | engineering classifier, router and orchestrator boundary; project isolation; SID identity, persona, wake words; secret redaction |
| `8e43a84` | Claude Code task history, test/build checks, timeout, token/cost capture, stricter tool bans, process-level tests |
| `61e6e2f` | original SID voice profile |
| `5b09c7a` | WebGL particle entity, state model, redesigned interface, Workbench, telemetry, UI tests |
| `2793797` | fixes found in live runs (typed-path boundary, check labels), faster voice DSP, README |
| `10b3e73` | wake word: Whisper's fused "Hesed" for "Hey Sid" |

## Architecture

- `sugar/intelligence/domain.py` — deterministic intent classifier (CODING_QA, PROJECT_ANALYSIS,
  BUILD_EXECUTION, NON_ENGINEERING, plus an `ambiguous` flag) and secret redaction.
- `sugar/intelligence/router.py` — every request, typed or spoken, is routed here. Only engineering
  fast-path commands run (project, Claude Code control, git diff, engineering memory). Desktop, media,
  clock, weather and similar grammar matches are classified as text and refused.
- `sugar/intelligence/orchestrator.py` — REJECTED: fixed sentence, no provider, no tool. CODING_QA: model
  with no tools (and the agent loop now ignores any tool call a model invents when none were offered).
  ANALYSIS/BUILD: `claude.task` only with an explicit project; otherwise SID asks and resumes the pending
  request when the user names a project. Typed directories are created only inside
  `permissions.allowed_roots`. Destructive requests raise the tool's permission level, so the existing
  permission manager asks before Claude Code starts.
- `sugar/coding/sessions.py`, `claude_code.py` — per-task history, checks derived from the exit status of
  test/build commands, `task_timeout_s` watchdog, token usage and cost only when the CLI reports them,
  disallowed tool rules, newline-safe arguments for `.cmd` CLI wrappers.
- `sugar/intelligence/working.py` — the active project is never restored at start-up (`previous_project`).
- `sugar/audio/voice_profile.py` — register shift + phase-vocoder pitch step + comb resonance + AM grain +
  EQ + compression, at 22.05 kHz.
- `sugar/ui/web/` — `entity.js` (Three.js r169, vendored, SHA-256 checked against two CDNs),
  `entity-state.js`, `render-loop.js`, `workbench-model.js`, `fallback-entity.js`, `highlight.js`, new
  `index.html`, `styles.css`, `app.js`.

## Verification

### Automated

| | Before | After |
|---|---|---|
| Python suite (`pytest --ignore=tests/test_desktop_live.py`) | 347 passed, 2 skipped | **456 passed, 2 skipped, 0 failed** |
| JavaScript units (`node --test tests/ui/*.test.mjs`, also run inside the suite) | — | 26 passed |
| Opt-in model tests (`SUGAR_MODEL_TESTS=1`, real Whisper + MeloTTS on CUDA) | not run in baseline | 15 passed |
| Ruff | — | clean |

New test files: `test_engineering_boundary.py` (classifier, end-to-end boundary, isolation, pending
resume, typed directories, restart), `test_claude_runner_process.py` (real subprocess against
`fake_claude.py`: streaming, resume, per-project cwd, cancellation kills the process, timeout, crash,
failing tests, start-up failure, destructive-rule lifting), `test_voice_profile.py`, `test_ui_js.py`,
`test_ui_browser.py` (shipped page in headless Edge over DevTools: WebGL init, every entity state from
bus events, streaming with highlighted code, refusal styling, Workbench expansion and steps, cost shown
only when reported, telemetry, resize, hidden window stops rendering, offline).

Tests changed on purpose, because the behaviour they pinned was removed by the product definition:

- `test_router.py`: chat/agent/reasoning routes → SID routes; desktop commands now asserted *refused*.
- `test_computer_grammar.py`: the Notepad end-to-end and "stop pauses the music" tests became "the
  desktop backend receives nothing" tests. The grammar unit tests are unchanged.
- `test_conversation.py`: wake words, typed refusal + programming answer, destructive-task confirmation
  (replacing the generic sensitive-tool test), stop and barge-in now use engineering questions.
- `test_memory.py`: the active project is no longer restored after a restart.
- `test_foundation.py`: default TTS speed 1.1 → 0.97.
- `test_tts.py` (model test): synthesis and voice DSP timed separately, best of three.

No regressions in tests that were not deliberately changed.

### Live, on this machine

- **Real app start**: `main.py --no-voice` opened the pywebview window titled "SID"; the WebGL entity
  rendered in WebView2, header showed NO ACTIVE PROJECT, Claude Code linked; closed cleanly by WM_CLOSE.
  With voice: all components ready in 19.6 s (Whisper large-v3-turbo + small.en on CUDA, MeloTTS on CUDA,
  Jabra microphone). Ollama was not running; FreeLLMAPI was.
- **Claude Code (real CLI 2.1.326)**: "Create a minimal Python command-line app with one pytest test in
  D:\Sugar_Models\tmp\sid-smoke-…" → directory created, Claude Code wrote `app.py` and `test_app.py`,
  test run detected and reported passed, cost $0.178 and token usage as reported by the CLI, 13.2 s.
  "Analyse the architecture of this project" → plan mode, same Claude session resumed, files verified
  unchanged by hash. "Tell me a joke about Java" → refused, no third run.
- **Programming answer**: FreeLLMAPI (gemini-2.5-flash), first token 1.3 s, first audio 364 ms.
- **Barge-in with a real person**: someone in the room spoke over SID's answer ("What is that sound?");
  playback stopped in 2 ms, the utterance was transcribed in 153 ms and refused as non-engineering
  (voice-to-voice 770 ms). The UI moved through speaking → interrupted → listening → processing →
  speaking → idle from real events.
- **Voice profile**: 12 synthesised sentences each way — median F0 203 Hz → 127 Hz; Whisper small.en WER
  1.6% (raw) vs 1.0% (SID); ~40 ms DSP per sentence. Samples: `data/voice_samples/`.
- **Wake phrase through real Whisper** (synthesised speech): "Sid, what is a closure…" and "Hey, Sid."
  detected; "Hey Sid, run the tests." was transcribed as "Hesed, run the tests." — now handled; "left
  side" and "I said…" do not wake.

### Bugs found by the live runs and fixed

- A typed directory ran past the sentence ("…sid-smoke-1. Keep it under 30 lines in total") and was
  created with that name. Paths now stop at clause boundaries; regression tests added; the stray
  directory (in `D:\Sugar_Models\tmp`) was removed.
- Workbench showed raw heredoc commands as the test step; it now shows Claude Code's step description.
- `.cmd` CLI wrappers truncate arguments at newlines (found with the stand-in CLI); arguments are now
  flattened for `.cmd`/`.bat` executables. The installed CLI is `claude.exe`, which was unaffected.

## Acceptance criteria

| # | Criterion | Status |
|---|---|---|
| 1 | Identifies exclusively as SID | Met for everything user-facing (window, page, wordmark, voice lines, Claude session names, docs). The Python package and data files keep the `sugar` name internally. |
| 2 | No hardcoded Q-Shield | Met. It was never hardcoded in code; it came from restored state, which no longer happens. Q-Shield remains only as a discovered project on disk and in test fixtures. |
| 3 | Handles engineering questions | Met (live). |
| 4 | Uses Claude Code for building | Met (live, real CLI). |
| 5 | Non-engineering can't invoke Claude Code | Met (tests assert no model, tool or CLI call; live check). |
| 6 | Accurate, isolated project context | Met (tests: named project wins, restart, per-project cwd in a real subprocess; live). |
| 7 | Real-time, interactive 3D entity | Met (WebGL, ~24k particles, pointer parallax, click pulse). |
| 8 | Entity reflects genuine states | Met (states derived only from bus events; browser test + live). |
| 9 | Interface redesigned around the entity | Met. |
| 10 | Workbench shows real Claude Code activity | Met (browser test with bus events; live session data). |
| 11 | Distinctive original voice | Met, with the limits below. |
| 12 | Wake word updated | Met; verified through Whisper on synthesised speech, not yet by a person saying "Hey SID". |
| 13 | Interruption still works | Met (suite + live barge-in by a real person). |
| 14 | Suite passes or failures documented | 456 passed, 2 skipped (opt-in model tests, which pass when enabled). |
| 15 | Starts and runs on this machine | Met (real window, voice pipeline, Claude Code). |

## Limitations and open items

- Not tested live: a person saying "Hey SID" into the microphone; real-CLI cancellation and timeout
  (those paths are tested against the stand-in CLI); `tests/test_desktop_live.py` (desktop control is
  disabled in SID).
- The classifier is rule-based and fails safe (refusal or a question, never execution), but unusual
  phrasing can be misclassified. A person reacting to the new voice asked "What is that sound?" — the
  voice is clearly synthetic by design; `sid_clean` and `natural` profiles are one click away.
- MeloTTS synthesis time varies with the laptop GPU's power state (0.12–0.5 s per sentence observed).
- In narrow windows (< 980 px) the entity shares the stage with the transcript and is drawn smaller.
- Desktop/media modules stay in the repository, unreachable from routing (`computer.backend` defaults to
  `none`).
