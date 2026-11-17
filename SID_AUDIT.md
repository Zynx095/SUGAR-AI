# SID — readiness audit
> Historical: this audit predates the universal transformation. Current status is in
> [SID_FINAL_ACCEPTANCE_REPORT.md](SID_FINAL_ACCEPTANCE_REPORT.md).

Date: 18 November 2026 · Branch `sid/transform` (12 commits on top of `rework/sugar-v2`, not merged or
pushed) · 76 files changed, +10.5k / −1.6k lines.

## Verdict

**Overall readiness: 6.5 / 10 — a strong personal beta.** Typed use, Claude Code work, the interface and
the cloned voice are in good shape and verified on this machine. Two things keep it from being a daily
driver you can leave listening all day: the wake phrase has not yet been confirmed with your own voice
after the latest fix, and the new "full power" setting lets the agent run PowerShell without asking, which
is exposed to prompt injection from web pages and files.

| Area | Score | One-line reason |
|---|---|---|
| Interface and 3D entity | 8 | Real WebGL, real states, tested in a browser and the real window |
| Claude Code integration | 8 | Real CLI verified end to end; cancel/timeout verified only against a stand-in |
| Project isolation | 8 | No stale project at start, explicit selection, per-project working directory |
| Test coverage | 8 | 489 + 17 model + 26 UI tests; live desktop tests not re-run since desktop control returned |
| Voice output (cloned) | 7.5 | 0.89 similarity to your clip, intelligible; reference clip is only 3.5 s |
| Request routing | 7 | Deterministic and tested; rules, not understanding, so odd phrasing misroutes |
| Speech flow | 7 | Gaps halved; still sentence-chunked synthesis |
| Telemetry | 7 | Measured values only; no history or export |
| Desktop control | 6 | Fast commands work; agent quality depends on the free model; not live-tested this round |
| Voice input / wake phrase | 5 | Whisper doesn't know "SID"; matched by its mishearings; unconfirmed with your voice |
| Safety | 5 | Destructive actions confirmed; sensitive actions auto-run, including from injected text |
| Documentation | 6 | README current; `SID_ENGINEERING_REPORT.md` describes the earlier coding-only design |
| Environment and operations | 5 | C: has ~490 MB free, Ollama off, no CI, branch unmerged |

## What is done

**Identity and routing**
- Every request (voice or typed) is classified locally: programming question, repository analysis,
  build work, desktop control, or refused. Small talk and general-knowledge questions get one fixed
  sentence and never reach a model, tool or Claude Code.
- Desktop control (on by default) runs grammar commands directly and sends free-form PC tasks to the agent
  with the full computer toolset. Turning it off restores the strict coding-only mode (still tested).

**Claude Code**
- Uses the installed CLI (2.1.326), resumes sessions, streams steps, records duration, cost and token
  counts only when reported, detects test/build pass or fail from exit codes, times out after 30 min.
- Commit, push and merge only when asked; recursive deletes and history rewrites only after you confirm.
- Live: built a small app with a passing test ($0.18, 13 s), then a read-only analysis resumed the same
  session without touching a file.

**Projects**
- Starts with NO ACTIVE PROJECT; uses a project you opened this session, one you name, or a folder you
  type (only inside allowed folders); asks otherwise and carries on when you answer.

**Interface**
- Three.js particle entity (~24k particles: core, five orbits, field, streams, waves) in crimson and dark
  red on pure black; eleven states driven only by real events; stops rendering when hidden; 2D fallback.
- Workbench (project, Claude Code status, live steps, checks, cost/tokens or "Not reported"), transcript
  with highlighted, copyable, collapsible code, telemetry panel.

**Voice**
- Output: MeloTTS converted by OpenVoice V2 to the speaker in `ULTRON_SID.mp3`; similarity 0.89 (raw
  MeloTTS 0.19), pitch 81 Hz vs 83 Hz, every band within 2.7 dB; only pace and treble fitted. Falls back
  to a DSP approximation if the model files are missing.
- Flow: chunk padding trimmed, punctuation-sized pauses, long pauses shortened, multi-sentence chunks.
- Input: "Hey SID" plus the forms Whisper actually produced from your microphone ("He said.", "acid",
  "Hesed"); speech is no longer cut off at the first pause; barge-in stops speech in ~2 ms.

**Desktop**
- Apps, windows, typing, shortcuts, browser, YouTube and Spotify search and playback, files, clipboard,
  screenshots, PowerShell. Live: answered "free space on D:" by running PowerShell itself (316 GB of 702 GB).

## Risks, in order

1. **Prompt injection with auto-approved PowerShell.** `permissions.auto_approve_level` is 2, so a model
   can run `terminal.run` without asking. Web pages, files and Claude Code output reach the same model.
   A malicious page read during a task could ask it to run a command, and nothing would stop it unless
   the command counts as destructive. *Fix:* auto-approve only commands typed or spoken by you, or only
   while no untrusted content is in the turn; or go back to level 1.
2. **Wake phrase unproven with your voice.** The fix is based on your logs and passes tests, but nobody has
   said "Hey SID" to the running app since. It can also wake on sentences containing "he said".
3. **C: drive almost full** (~490 MB). Windows updates, the WebView2 runtime or Python temp files can fail
   unpredictably. Not caused by SID, but it affects SID.
4. **Single 3.5-second voice reference.** The clone is close on that sentence; other sentences may drift.
5. **Free model dependence.** Programming answers and the desktop agent use FreeLLMAPI (first token
   ~0.8–2.4 s); Ollama, the offline fallback, wasn't running.
6. **Live desktop tests not re-run.** `tests/test_desktop_live.py` drives the real desktop and wasn't run
   while you were using the PC (Valorant was open).

## What could be improved

**Next (high value, small effort)**
- Make auto-approval source-aware (risk 1).
- Say "Hey SID" ten times with the app running and read the `STT_FINAL` lines in `data/logs/sugar.jsonl`;
  add any new mishearings. Optionally train a small custom wake-word model (openWakeWord) for "Hey SID".
- Record 20–30 s of the reference voice and run `tools/fit_clone.py`.
- Free space on C: (pip cache is 2.1 GB, `%LOCALAPPDATA%\Sugar` 71 MB is the unused old window profile).
- Update or archive `SID_ENGINEERING_REPORT.md` (it predates desktop control and the cloned voice).

**Soon**
- Run the live desktop tests at a quiet moment; add live checks for YouTube/Spotify search.
- Re-enable "stop" pausing your music when SID has nothing else to stop (unwired in the coding-only phase).
- Stream synthesis inside a sentence (start audio before the whole chunk is synthesised) to cut first audio.
- Start Ollama (or another local model) so answers and desktop control work offline.
- Let small talk through when desktop control is on, if you want it (one setting).

**Later**
- Rename the internal `sugar` package and data files to `sid`.
- A learned intent classifier with the rule-based one as a safety net.
- CI that runs the suite, the JS tests and the headless UI test on every commit.
- Telemetry history and export; GPU memory monitor (Whisper + MeloTTS + OpenVoice share 8 GB; not measured
  under load).
- Narrow-window layout gives the entity more room.
- Merge `sid/transform` once the wake phrase and safety items are done.

## Evidence

- `pytest --ignore=tests/test_desktop_live.py`: **489 passed, 3 skipped** (the 3 are opt-in model tests).
- `SUGAR_MODEL_TESTS=1 pytest tests/test_tts.py tests/test_stt.py`: **17 passed** (real Whisper, MeloTTS,
  OpenVoice).
- `node --test tests/ui/*.test.mjs`: **26 passed**.
- Screenshots: `data/ui_screens/`; voice samples: `data/voice_samples/`; logs: `data/logs/sugar.jsonl`.
