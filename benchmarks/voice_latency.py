"""Voice pipeline component latency: fast path, Whisper (partial + final), MeloTTS, FreeLLMAPI first token.

    python benchmarks/voice_latency.py            # everything
    python benchmarks/voice_latency.py --no-llm   # skip the network model

Uses the bundled speech sample (tests/data/speech_sample.flac) and the models
configured in settings (GPU when available). Loads Whisper and MeloTTS, so
don't run it while something else needs the GPU.
"""

from __future__ import annotations

import argparse
import asyncio
import time

from _common import ROOT, measure, ms_since, row, save, table

from sugar.config.settings import load_settings

UTTERANCES = [
    "open notepad", "type hello this is sugar", "close this tab", "play blinding lights by the weeknd on youtube",
    "what time is it", "volume 40", "switch to notepad", "open notepad and type hello", "search youtube for lofi",
    "pause", "minimize chrome", "go back", "close notepad", "tell me a joke about compilers",
    "open my jiva project", "what's the weather in chennai",
]


def fast_path_rows() -> list[dict]:
    from sugar.intelligence.fastpath import DesktopView, FastPath

    view = DesktopView(active_app="brave", active_is_browser=True, titles=("lofi - youtube - brave",))
    known = {"notepad", "chrome", "brave", "spotify"}
    fastpath = FastPath(resolve_app=lambda n: n if n in known else None, desktop=lambda: view)
    samples, _ = measure(lambda: [fastpath.match(u) for u in UTTERANCES], repeat=200, warmup=20)
    per_call = [s * 1000 / len(UTTERANCES) for s in samples]  # µs
    return [{"name": "Fast-path grammar match (per utterance)", "stats": {
        "n": len(per_call), "median": round(sorted(per_call)[len(per_call) // 2] / 1000, 3), "p90": "", "min": "",
        "max": ""}, "samples": [], "notes": f"{len(UTTERANCES)} utterances incl. compound commands; value in ms"}]


def stt_rows(settings) -> list[dict]:
    import soundfile as sf

    from sugar.audio.stt import Evidence, SpeechRecognizer

    audio, rate = sf.read(str(ROOT / "tests" / "data" / "speech_sample.flac"), dtype="float32")
    if rate != 16000:
        import soxr

        audio = soxr.resample(audio, rate, 16000)
    recognizer = SpeechRecognizer(settings.stt, settings.paths.whisper_dir)
    start = time.perf_counter()
    recognizer.load()
    load_ms = ms_since(start)
    fast, _ = measure(lambda: recognizer.transcribe(audio, final=False, evidence=Evidence()), repeat=5, warmup=1)
    final, transcript = measure(lambda: recognizer.transcribe(audio, final=True, evidence=Evidence()), repeat=5,
                                warmup=1)
    seconds = len(audio) / 16000
    return [row(f"Whisper partial ({recognizer.fast.name}, {recognizer.device})", fast, f"{seconds:.1f} s clip"),
            row(f"Whisper final ({recognizer.final.name}, {recognizer.device})", final,
                f"{seconds:.1f} s clip → “{transcript.text[:50]}”"),
            {"name": "Whisper load (both models)", "stats": {"n": 1, "median": round(load_ms, 1)}, "samples": [],
             "notes": "once at start-up, in the background"}]


def tts_rows(settings) -> list[dict]:
    import tempfile
    from pathlib import Path

    from sugar.audio.tts import build_synthesizer

    synth = build_synthesizer(settings.tts, Path(tempfile.mkdtemp()))
    synth.load()
    sentences = ["Notepad's open.", "I found the problem in the login handler.",
                 "Playing Blinding Lights by The Weeknd on YouTube."]
    rows = []
    for sentence in sentences:  # uncached synthesis (the phrase cache would make repeats free)
        samples, _ = measure(lambda s=sentence: synth._synthesize_with_fallback(s), repeat=5, warmup=1)
        rows.append(row(f"TTS “{sentence[:32]}”", samples, f"{synth.engine_name}, uncached"))
    return rows


async def llm_rows(settings) -> list[dict]:
    from sugar.core.events import EventBus
    from sugar.providers import build_provider_pool

    pool = build_provider_pool(settings, EventBus())
    samples, routed = [], set()
    for prompt in ("Say hi in three words.", "What is 17 times 3? Answer with the number.", "Name one planet."):
        for _ in range(2):
            start = time.perf_counter()
            first = None
            async for chunk in pool.stream(["freellm"], [{"role": "user", "content": prompt}], purpose="chat"):
                if chunk.kind == "meta" and chunk.data.get("model"):
                    routed.add(chunk.data["model"])
                if chunk.kind == "text" and first is None:
                    first = ms_since(start)
            if first is not None:
                samples.append(first)
    await pool.aclose()
    return [row("FreeLLMAPI time to first token (chat)", samples, "routed: " + ", ".join(sorted(routed))[:80])]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--no-models", action="store_true", help="skip Whisper and MeloTTS")
    args = parser.parse_args()
    settings = load_settings()
    rows = fast_path_rows()
    if not args.no_models:
        rows += stt_rows(settings)
        rows += tts_rows(settings)
    if not args.no_llm:
        try:
            rows += await llm_rows(settings)
        except Exception as exc:
            print("LLM benchmark skipped:", exc)
    print(table(rows))
    print("saved", save("voice_latency", rows))


if __name__ == "__main__":
    asyncio.run(main())
