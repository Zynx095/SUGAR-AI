# SID — Desktop Intelligence, Voices and Personalities

The upgrade on branch `sid/desktop-intelligence`, built on the universal SID (recovery point: tag
`sid-checkpoint-before-desktop`). Status labels:

- **VERIFIED**: exercised on this PC with the real component (model, OS API, browser or network).
- **IMPLEMENTED**: code plus automated tests against fakes.
- **PENDING**: a live run that needs the PC idle or the user present.
- **NOT IMPLEMENTED**: not built.

## 1. Voices

| Voice | How it is made | Measured (`tools/evaluate_voices.py`, real engine) | Palette |
|---|---|---|---|
| SID (original) | MeloTTS re-voiced by OpenVoice as `ULTRON_SID.mp3` | F0 79 Hz, range 5.9 st, WER 7 % | amber / orange |
| SID (female) | MeloTTS EN-Newest speaker, light EQ | F0 201 Hz, range 3.4 st, WER 2 % | crimson / red |
| **SID (blue)** | **MeloTTS English v1 EN-AU speaker** (a different speaker and accent), formants lowered 8 %, time-varying PSOLA that raises the intonation range ×1.12 and pulls each pitch 60 % toward the nearest semitone (a tuned, playful robot), a 1.6 ms "small metal speaker" comb, an 80 Hz ring-modulated buzz at 25 % depth, 1.6× pauses for comic timing | F0 138 Hz, range 17 st, **speaker similarity 0.39 to female** (the old third voice: **0.86**), 0.52 to SID, WER 2 % | electric blue `#1e90ff`, ice `#c8f4ff`, cobalt `#0b2a8f`, cyan `#00e5ff` |

How the blue voice was chosen: candidates were rendered and measured on pitch, intonation range, OpenVoice
speaker-embedding similarity to the female voice and to SID, and Whisper intelligibility. The chosen one is
the most distinct from both existing voices that Whisper still transcribes word for word. The earlier
"neutral" identity id now maps to blue (old saved settings keep working).

Status: **VERIFIED**. This covers the real-engine measurements and a model test
(`SUGAR_MODEL_TESTS=1 pytest tests/test_tts.py`) that asserts the blue voice's similarity to the female voice
is below 0.6, that the two are more than 3 semitones apart and that Whisper hears the words.

Whether the blue voice sounds funny is the user's call: the measurements say it is distinct and intelligible,
not that it is charming.

## 2. Personality engine

Code: `sugar/intelligence/personality.py`. Personalities: `sid`, `female`, `blue`, `professional`,
`friendly`, `creative`, `cinematic`. Each one sets the following:

- A style prompt (vocabulary, sentence construction, humour, rhythm), with tone samples that the model imitates
  without repeating them.
- The typical reply length.
- The model temperature (blue 0.9, professional 0.3).
- The voice delivery: pace, and the pause between sentences.
- Its action lines ("Paint. A bold choice. Opening it now.").
- How it behaves in roleplay.

Sober personalities keep the plain, verified confirmations ("Notepad's open.").

Every personality turns serious for health, emergencies, loss, safety, legal or money trouble. The model is
told "no jokes, no sarcasm", the temperature is capped at 0.4, and action lines stay plain.

How a personality is selected:

| Kind | Example | Lasts |
|---|---|---|
| Per-voice default | the SID voice brings `sid`, female brings `female`, blue brings `blue` | saved |
| Pinned | "be friendly from now on", or the settings menu | saved for that voice |
| Temporary | "switch to your blue personality", "use your funny personality", "speak more professionally" | this session (survives voice changes) |
| For a moment | "be serious for a moment" | the next three answers |
| Trait dials | "be more sarcastic", "tone down the sarcasm", "fewer jokes", "be more concise" | this session |
| Reset | "go back to your normal personality" (the voice's own); "go back to your original personality" (`sid`) | |

An older saved personality is migrated to the voice that was in use when it was saved.

Live check: the same questions were asked of the real FreeLLMAPI model under each personality.

- **Blue**: "Ah, the eternal question… the computer has to look at every. single. item. It's like checking every
  seat in a stadium for one specific fan." and "The Moon is Earth's only natural satellite. A loyal companion,
  indeed."
- **Professional**: "…due to its underlying implementation as a dynamic array. The `in` operator performs a
  linear search…"
- **Blue with a health question** ("My dad was just diagnosed with diabetes"): a factual, joke-free answer.

Status: **VERIFIED** with the live model, plus 14 automated tests.

The friendly and female personalities came out less distinct in that live sample. FreeLLMAPI routes to
different backend models, so how strongly a style shows depends on which model answered.

## 3. Desktop control

| Capability | How | Status |
|---|---|---|
| Open apps ("open Paint/Notepad/Settings/File Explorer/Task Manager/Calculator/Control Panel/Brave/VS Code") | Start-menu apps with AUMIDs, built-ins, **App Paths registry and execution aliases** (new), aliases ("ms paint"); not installed → "I can't find an app called Spotify on this PC" (no improvising) | resolution VERIFIED against this PC's catalogue; launching IMPLEMENTED, live run PENDING |
| Settings pages ("open Bluetooth settings", "display settings", "settings for sound"; 60+ pages) | `ms-settings:` URIs, navigating an already-open Settings window | IMPLEMENTED, live PENDING |
| Windows: switch, minimise, maximise, restore, snap left/right, close, new window, what's active | existing grammar and tools; closing is ordinary (not destructive); an unsaved-changes prompt is read and answered ("save / don't save / cancel") | IMPLEMENTED (earlier live suite) |
| **Scrolling** ("scroll down", "a little", "more", "to the bottom/top", "scroll slowly", "keep scrolling", "stop scrolling", "in the left panel", "inside this document", "page down", "scroll down 3 times") | a fresh UIA snapshot picks the scrollable area (region, the focused document, or the largest); ScrollPattern scrolls **without touching the mouse**; otherwise wheel input over the area with the pointer put back; edge detection ("Already at the bottom"); continuous scrolling on its own thread stops on request, at the end, after 2 min, or when the user touches the PC | IMPLEMENTED; UIA snapshot VERIFIED on a real Brave window (60 elements in 0.22 s); live scroll PENDING |
| **Clicking by description** ("click Settings", "select the second option", "click the button labelled Save", "click the checkbox", "open the first result", "choose the option on the left", "click the search box") | parse (name, kind, ordinal, region) → fresh UIA snapshot (dialogs the window owns first) → exactly one match acts via Invoke/Toggle/Select/Expand (no mouse), else a mouse click at the element's current centre with the pointer restored → verified by toggle/selection/expansion/focus/new window/UI change; **two equal matches → "I see 2 matches: … Which one?"** and "the first one" answers; nothing → OCR words, then the vision model's pointing; still nothing → says so | IMPLEMENTED (28 tests), live PENDING |
| Selecting text ("select everything", "select the paragraph", "select the previous paragraph") | keyboard chords in the document | IMPLEMENTED |
| **Reading the screen** ("read the text in this window", "what is open on my screen", "where is the Save button", "what error is displayed", "explain what this dialog is asking", "what should I do next", "what can I click") | UIA text first; **Windows OCR** (WinRT, on-device, ~0.07 s) when that's thin; **local vision model** `qwen3-vl:4b` on the managed Ollama for questions; screenshots never leave the PC | OCR VERIFIED; vision VERIFIED on a synthetic dialog (described correctly; pointed to Save/Don't save/Cancel within 3 px; ~4 s warm, 18 s cold); live on real windows PENDING |

## 4. Dictation and writing

- **Dictation mode**: say "start dictation" (or "type what I say"). SID types each recognised sentence into the
  document in front, adding a joining space and capitals.
  - "Go to the next line" presses Enter and "new paragraph" leaves a blank line. They are never typed as words.
  - Spoken punctuation works: "comma", "period", "question mark".
  - Edits are commands: delete the last sentence, scratch that, undo, redo, select the (previous) paragraph,
    go to start/end, replace X with Y, clear, indent, bullets, numbered list, write this exactly, correct the
    spelling.
  - Headings work only where the app has them (Word: Ctrl+Alt+1). In Notepad SID says it is plain text instead
    of pretending.
  - SID doesn't chat back. "Hey SID, …" still reaches SID, and "stop dictation" ends the mode. A bare "stop"
    interrupts typing.
- **Writing mode**: "write a paragraph about artificial intelligence" with an editor in front (or "… in Notepad",
  which opens it). SID streams the model's text into the document as it is written. "continue",
  "go to the next line" and the edits then apply to that document. "Open Notepad and write …" works as one
  command.
- **Focus guard**: typing happens only while the target window is in front. If you switch apps, typing stops
  and SID says so. It never refocuses the window and types somewhere you didn't expect.
- **Indicators**: a "Dictating into Notepad" badge, and the entity's dictating/writing state.

Status: **IMPLEMENTED** (32 tests). These include the next-line regression, focus loss, and streaming that
reaches the document before the model finishes. Live run in Notepad: PENDING.

## 5. Drawing in Paint

1. **Plan**:
   - Simple subjects (circle, square, triangle, star, heart, house, sun, smiley, tree, flower, arrow) have
     vector plans.
   - Anything else is traced from a reference image. OpenCV separates the subject from the background, finds the
     outline and the main inner lines, simplifies them to at most 1,400 points and keeps the proportions.
2. **Reference**:
   - Searches Wikimedia Commons with four phrasings ("cat line art", "silhouette", "clipart", "drawing").
   - Keeps only free licences (CC0, public domain, CC BY, CC BY-SA) and downloads only PNG/JPEG renditions
     under 3 MB, never other file types.
   - Scores how drawable each one is (a clean subject on a plain background beats a busy photo). When the vision
     model is running, it asks whether the best ones really show the subject.
   - The title, author, licence and page are shown with the result.
   - Live for "cat": 37 candidates in 4.7 s. Top picks: "Line-art drawing of a cat" (CC BY-SA 4.0) and
     "Art Nouveau stretching cat.svg" (public domain), both scoring 0.94.
3. **Draw**:
   - Opens or focuses Paint and finds the canvas in a fresh screenshot.
   - Picks the pencil, colour and size through UI Automation.
   - Draws with real mouse drags in 5 px steps, using SendInput absolute moves.
   - Before each stroke it checks that Paint is still in front. If the pointer is anywhere SID didn't put it, it
     stops with the button released.
   - Verifies that the canvas changed.
4. **Follow-ups**:
   - "draw a tree beside it" (placed in the remaining room)
   - "add colour" or "make it red" (Paint's fill tool at a point inside the outline)
   - "make the outline thicker"
   - "undo the last shape" (as many Ctrl+Z as that shape took)
   - "make it cleaner" (redraw with fewer, smoother lines)
   - "start a new drawing"
5. **Save**: "save this as cat.png" saves to `Pictures\SID\cat.png` through Paint's Save dialog. Replacing an
   existing file is a destructive action, so the security policy always asks first.

Nothing is pasted or overlaid: every line is drawn by Paint.

Status: **IMPLEMENTED** (25 tests). Reference search and tracing are **VERIFIED** with the live network. Drawing in
the real Paint is PENDING (it needs the mouse). Paint's exact UI Automation names for its tools are assumed
("Pencil", "Fill", colour names, "Size") until the live run confirms them.

## 6. Camera

- **Discovery**: Windows PnP names plus the OpenCV indices that open. This PC has "Integrated Camera" and
  "Chicony USB 2.0 Camera".
- **Turning it on**: "turn on the camera" opens the device and shows a red **CAMERA ON · device** badge. The
  entity tints red while a frame is being looked at.
- **Asking**: "what do you see?" and "describe this object" grab a fresh frame (after the exposure settles) and
  describe it with the local vision model.
- **Photos**: "take a picture" is the only action that saves an image, to `data/camera/`.
- **Turning it off**:
  - "turn off the camera" releases the device.
  - It also releases after 2 idle minutes and at shutdown.
  - A one-off look releases it straight away.
- **Never implicit**: with the camera off, "what do you see?" describes the screen and does not switch the camera
  on. Camera tools are never offered to a model; only your own words reach them.

Status: **IMPLEMENTED** (lifecycle, idle release, badge, indicator; tests with a fake device, and the badge in
headless Edge). The live camera is PENDING. It was deliberately not switched on while the PC was unattended;
see the acceptance report.

## 7. Permissions and provenance

- Ordinary operations you asked for run without confirmation: open, close, switch, scroll, click ordinary
  controls, select, type, navigate, draw, save a new file.
- Confirmation stays for: deleting, overwriting (Paint's save), recursive deletion, privileged commands,
  clicking a control named Delete/Remove/Buy/Send/Publish…, and anything proposed after untrusted content
  was read.
- New untrusted origins: **SCREEN_CONTENT** (UIA text, OCR, vision descriptions) and **CAMERA_IMAGE**. A
  test shows that text on the screen or in front of the camera saying "run Remove-Item…" can't run anything.
  It isn't even offered for confirmation.
- Drawing references are web content: only image bytes are used, and page text never reaches a model.

## 8. State and interruptions

- One action thread: all desktop work runs on the single `sugar-desktop` worker.
- What "stop" does:
  - stops speech, then the turn, then typing (dictation stays on)
  - stops continuous scrolling
  - stops a drawing at the next stroke
  - "stop dictation" ends the mode
- New entity states: dictating, writing, seeing (reading the screen), camera, drawing, browsing (fetching a
  reference). They are driven by `activity`, `dictation.state` and `camera.state` events. Speech always wins.
  The state is restored from the snapshot on reconnect.
- Context awareness:
  - "draw a circle" with Paint open goes to Paint.
  - "next line" with Notepad in front presses Enter.
  - "scroll down" scrolls whatever is in front.
  - "Open Paint and draw a cat" carries Paint into the second clause.

## 9. Storage

All new downloads went to D:

| Item | Location | Size |
|---|---|---|
| `qwen3-vl:4b` | `D:\Sugar_Models\ollama\models` | 3.3 GB |
| MeloTTS English v1 (blue voice) | `D:\Sugar_Models\hf` | 199 MB |
| MeloTTS English v2 (measured during voice selection, unused, removable) | `D:\Sugar_Models\hf` | 199 MB |
| OpenCV and WinRT OCR packages | the venv on D: | — |

C: free space went from 2.86 GB to 3.01 GB during this work (nothing written there by SID).

## 10. Tests

| Suite | Files | Count |
|---|---|---|
| Personality | `tests/test_personality.py` | 14 |
| Interaction, scrolling, camera, screen/camera security | `tests/test_interaction.py` | 28 |
| Dictation | `tests/test_dictation.py` | 32 |
| Paint and references | `tests/test_paint.py` | 25 |
| Blue voice (model test) | `tests/test_tts.py` | +1 |
| UI unit tests | `tests/ui/entity-state.test.mjs` | +2 |
| Headless browser | `tests/test_ui_browser.py` | camera and dictation badges |

Live desktop: `tools/desktop_acceptance.py` waits for an idle PC with no full-screen app, then drives the real
pipeline. Results go to `data/acceptance/`. The one partial live run and its findings are in
[SID_DESKTOP_ACCEPTANCE_REPORT.md](SID_DESKTOP_ACCEPTANCE_REPORT.md).
