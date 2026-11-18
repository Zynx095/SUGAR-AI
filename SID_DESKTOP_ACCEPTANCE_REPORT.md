# SID — Desktop Intelligence Acceptance Report

Branch `sid/desktop-intelligence` (not pushed). Recovery point: tag `sid-checkpoint-before-desktop`.
Feature details: [SID_DESKTOP_INTELLIGENCE.md](SID_DESKTOP_INTELLIGENCE.md).

Status words:

- **VERIFIED**: run on this PC with the real component.
- **AUTOMATED**: unit or integration tests against fakes pass.
- **NOT RUN**: the live check was skipped at the user's request, to be reviewed by the user.
- **FAILED → FIXED**: failed in the live run; fixed afterwards but not re-run live.

## Automated suites (all passing)

- **Python**: the full suite (`pytest --ignore=tests/test_desktop_live.py`). New files:
  - `test_personality.py`
  - `test_interaction.py`
  - `test_dictation.py`
  - `test_paint.py`
- **Model test**: the blue voice against the female voice (`SUGAR_MODEL_TESTS=1`).
- **UI**: node tests (28) and the headless Edge test, which checks the camera and dictation badges.

## Live desktop run (19 Nov 2026, 00:05–00:12, partial)

`tools/desktop_acceptance.py` ran once while the PC was idle. It was cut short and showed real problems.

| Step | Result |
|---|---|
| "open settings" | **VERIFIED** ("Settings's open.") |
| "open bluetooth settings" | **FAILED → FIXED**. It reported "File Explorer's open." Settings pages weren't matched to the Settings window, so SID took another new window for it. Pages now match the Settings window. |
| scroll / "what can I click" / "where is the search box" / "read the text in this window" | **FAILED → FIXED**. Each step's mechanics worked, but on the wrong window: the terminal the run was started from. With no SID window, that terminal counted as "the window in front". |
| "open notepad" | **VERIFIED** |
| "start dictation" + dictated sentences | **FAILED → FIXED** (same cause). Notepad lost the foreground, so typing stopped by design and dictation ended. |
| "write a short paragraph about AI" | **FAILED → FIXED** (same cause). With the terminal in front, it was answered in chat instead of being written into Notepad. |
| "close this" | **FAILED → FIXED, and it did harm**: it closed the terminal window the run was started from, which ended that Claude Code session. Fix: in headless and text runs, the terminal SID starts from is protected, and SID never treats it as "this window". A test covers it. |
| Paint, camera | **NOT RUN** (the run ended first; camera live tests were not planned without you present) |

## Final acceptance criteria

| # | Criterion | Status |
|---|---|---|
| 1 | Blue voice genuinely different from female | VERIFIED: speaker similarity 0.39 (was 0.86), F0 138 vs 201 Hz, WER 2 % |
| 2 | Blue personality genuinely funny and distinct | VERIFIED with the live model ("every. single. item."); funniness is yours to judge |
| 3 | Personality switching changes actual responses | VERIFIED live (blue vs professional vs friendly) + AUTOMATED |
| 4 | Voice and entity colours synchronise | AUTOMATED (`voice.changed` carries the palette; browser test) |
| 5 | Open Paint by voice | AUTOMATED; NOT RUN live |
| 6 | Open Windows Settings by voice | VERIFIED live |
| 7 | Scroll naturally | AUTOMATED; live run hit the wrong window (FIXED, NOT RUN again) |
| 8 | Identify visible controls | AUTOMATED; live listed controls of the wrong window (FIXED, NOT RUN again) |
| 9 | Select and click controls | AUTOMATED; NOT RUN live |
| 10 | Read visible screen content | OCR and vision VERIFIED on synthetic windows; live read the wrong window (FIXED, NOT RUN again) |
| 11 | Dictate incrementally | AUTOMATED (streaming reaches the document before the model finishes); live FAILED → FIXED |
| 12 | "Go to the next line" works | AUTOMATED (Enter, never the words); NOT RUN live |
| 13 | Editing commands work | AUTOMATED; NOT RUN live |
| 14 | Find a visual reference online | VERIFIED (Wikimedia Commons, 37 candidates for "cat", free licences only) |
| 15 | Genuinely draw in Paint from the reference | AUTOMATED (real drags, canvas detection, verification); NOT RUN live |
| 16 | Activate the camera on request | AUTOMATED (fake device); NOT RUN live |
| 17 | Describe a fresh camera frame | vision model VERIFIED on images; NOT RUN live with the camera |
| 18 | Camera indicator and clean shutdown | AUTOMATED + browser test (badge); NOT RUN live |
| 19 | No unnecessary confirmations for normal operations | AUTOMATED (policy: direct requests ≤ modification run) |
| 20 | Destructive operations and sensitive transfers protected | AUTOMATED (overwrite = destructive; screen/camera content can't authorise) |
| 21 | Universal intelligence still works | AUTOMATED (full suite) + live personality run |
| 22 | Claude Code intact | AUTOMATED (existing suites unchanged and passing) |
| 23 | Voice pipeline stable | AUTOMATED + model tests (TTS, blue voice) |
| 24 | Full suite passes or failures documented | passing; live failures documented above |

## To check yourself

The live items marked NOT RUN can be run with:

```powershell
.\venv\Scripts\python.exe tools\desktop_acceptance.py --wait-idle 0
```

Start it from a terminal. That terminal is now protected, so SID won't act on it. The run uses the mouse and
keyboard for a few minutes. It leaves an already-open Paint or Settings alone and closes only the Notepad tab it
wrote in. Add `--skip camera` to leave the camera off.
