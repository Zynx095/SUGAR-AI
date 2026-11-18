"""Structured wake-phrase test with the real microphone and the real recogniser.

Run it yourself (it records your voice):

    venv\\Scripts\\python.exe tools\\wake_test.py            # 10 wake trials + 10 control sentences
    venv\\Scripts\\python.exe tools\\wake_test.py --trials 20

For each trial it prompts you, records a few seconds, transcribes with the same Whisper models and the same
wake logic SID uses (look-alike transcripts are re-checked acoustically), and records: detected or not,
how it matched (exact / verified look-alike), the confidence of the transcript, and the decision latency.
Control trials are ordinary sentences that sound similar ("He said the build failed", "Hey, sit down");
any wake there is a false activation. The report is saved to data/wake_tests/<timestamp>.json.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from sugar.audio.stt import Evidence, SpeechRecognizer  # noqa: E402
from sugar.config.settings import load_settings  # noqa: E402
from sugar.intelligence.conversation import split_wake_word, wake_match_kind  # noqa: E402

WAKE_PROMPTS = ["Hey SID", "Hey SID, what time is it", "SID, run the tests", "Hey SID, open my project",
                "Hey SID, explain recursion"]
CONTROL_PROMPTS = ["He said the build failed", "I said we should leave", "Hey, sit down for a minute",
                   "That's what he said", "Hey, can you pass the salt"]


def record(seconds: float, rate: int = 16000, device=None) -> np.ndarray:
    import sounddevice as sd

    audio = sd.rec(int(seconds * rate), samplerate=rate, channels=1, dtype="float32", device=device)
    sd.wait()
    return audio[:, 0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--seconds", type=float, default=3.0)
    args = parser.parse_args()

    settings = load_settings()
    conv = settings.conversation
    stt = SpeechRecognizer(settings.stt, settings.paths.whisper_dir)
    print("Loading speech recognition…")
    stt.load()
    results = []
    plan = [("wake", WAKE_PROMPTS[i % len(WAKE_PROMPTS)]) for i in range(args.trials)]
    plan += [("control", CONTROL_PROMPTS[i % len(CONTROL_PROMPTS)]) for i in range(args.trials)]
    for index, (kind, prompt) in enumerate(plan, 1):
        input(f"\n[{index}/{len(plan)}] Press Enter, then say: \"{prompt}\"")
        audio = record(args.seconds, device=settings.audio.input_device)
        started = time.perf_counter()
        transcript = stt.transcribe(audio, final=True, evidence=Evidence(int(args.seconds * 1000), 0.9))
        wake, _ = split_wake_word(transcript.text, conv.wake_words, conv.wake_words_after_hey)
        match = wake_match_kind(transcript.text, conv.wake_words) if wake else None
        verified = None
        if wake and match == "variant" and conv.wake_verify:
            verified, _, _ = stt.verify_wake(audio)
            wake = verified
        latency = int((time.perf_counter() - started) * 1000)
        results.append({"expected": kind, "prompt": prompt, "heard": transcript.text, "detected": bool(wake),
                        "match": match, "verified": verified, "avg_logprob": round(transcript.avg_logprob, 3),
                        "latency_ms": latency})
        print(f"  heard: {transcript.text!r} → {'WAKE' if wake else 'ignored'} ({latency} ms)")

    wake_trials = [r for r in results if r["expected"] == "wake"]
    control = [r for r in results if r["expected"] == "control"]
    summary = {
        "detections": sum(r["detected"] for r in wake_trials), "missed": sum(not r["detected"] for r in wake_trials),
        "false_activations": sum(r["detected"] for r in control), "trials": len(results),
        "median_latency_ms": int(np.median([r["latency_ms"] for r in results])) if results else None,
    }
    out = settings.paths.data_dir / "wake_tests" / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "results": results}, indent=1), encoding="utf-8")
    print(f"\n{summary}\nSaved {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
