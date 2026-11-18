# Sugar AI — Latency & Performance Benchmark Report

**Benchmarked on:** November 17, 2026  
**Environment:** Windows 11 (Build 26200), AMD64, Python 3.12.0  
**Hardware:** NVIDIA GeForce RTX 5050 Laptop GPU (Blackwell architecture, sm_120)  
**PyTorch Build:** `torch 2.7.1+cu128` (CUDA 12.8)  

---

## 1. Executive Summary

Sugar is engineered for sub-second, voice-first desktop interaction. All measurements below were recorded on the physical hardware using the benchmark scripts in `benchmarks/` and real live execution turns.

```text
Voice Input (Silero VAD) ──> Whisper small.en (~178 ms partials)
                               │
                               v
                       Fast-Path Grammar (0.018 ms match) ──> Instant OS Execution (11 ms)
                               │
                               v
                    FreeLLMAPI Stream (TTFT ~1.09 s) ──> MeloTTS GPU (~60 ms / sentence) ──> Audio Playback (WASAPI)
```

---

## 2. Voice Pipeline Latency Measurements

Recorded with `python benchmarks/voice_latency.py` against the standard 6.7-second reference audio clip (`tests/data/speech_sample.flac`):

| Pipeline Stage | Engine / Model | Hardware | Median (ms) | p90 (ms) | Min (ms) | Max (ms) | Notes |
|---|---|---|---|---|---|---|---|
| **Fast-Path Grammar Match** | Deterministic Regex Grammar | CPU | **0.018 ms** | — | — | — | 16 sample commands tested 200× (18 microseconds!) |
| **STT: Real-Time Partial** | `small.en` (faster-whisper) | CUDA fp16 | **178.5 ms** | 185.9 ms | 173.7 ms | 185.9 ms | 6.7 s audio clip; runs while user is speaking |
| **STT: Final Transcript** | `large-v3-turbo` (faster-whisper) | CUDA fp16 | **342.8 ms** | 371.8 ms | 335.0 ms | 371.8 ms | Speculative start at pause overlaps endpoint window |
| **TTS: Short Phrase** | MeloTTS (`EN-Newest`) | CUDA | **60.7 ms** | 62.8 ms | 59.4 ms | 62.8 ms | *"Notepad's open."* (uncached) |
| **TTS: Medium Sentence** | MeloTTS (`EN-Newest`) | CUDA | **60.1 ms** | 68.6 ms | 55.7 ms | 68.6 ms | *"I found the problem in the login"* (uncached) |
| **TTS: Long Sentence** | MeloTTS (`EN-Newest`) | CUDA | **66.6 ms** | 69.7 ms | 62.3 ms | 69.7 ms | *"Playing Blinding Lights by The W..."* (uncached) |
| **LLM: Time-to-First-Token** | FreeLLMAPI (chat) | Localhost:31415 | **1,090.9 ms** | 1,229.4 ms | 737.4 ms | 1,389.1 ms | Routed to `google/gemini-2.5-flash` |
| **Speaker Audio Latency** | WASAPI shared stream | Default Device | **~23 ms** | — | — | — | Output buffer latency |

---

## 3. End-to-End Command Latency

Measured across live single-turn executions (`python main.py --text "<command>"`):

| Spoken Command | Subsystem | Turnaround Time | Model Used |
|---|---|---|---|
| *"what time is it"* | Fast-Path `system.time` | **12 ms** | None (0 ms LLM) |
| *"what is 42 * 17"* | Fast-Path `calc.evaluate` | **11 ms** | None (0 ms LLM) |
| *"hello"* | Conversational `chat` | **1,844 ms** | `google/gemini-2.5-flash` (via FreeLLMAPI) |

---

## 4. Media Resolution & Search Latency

Recorded with `benchmarks/media_resolution.py` across 18 YouTube search queries (6 distinct queries × 3 runs):

| Operation | Median (ms) | p90 (ms) | Min (ms) | Max (ms) | Notes |
|---|---|---|---|---|---|
| **YouTube Web Search** | **1,468.6 ms** | 1,513.6 ms | 973.0 ms | 1,674.0 ms | Scrapes and parses public results page |
| **Semantic Ranking & Filter** | **2.0 ms** | 3.7 ms | 0.5 ms | 5.8 ms | Penalizes covers/lyrics, scores channels & duration |
| **Rickroll Verification** | **< 1 ms** | 1.2 ms | 0.3 ms | 2.1 ms | Rejects false-positive video IDs |

---

## 5. Architectural Performance Conclusions

1. **Deterministic Fast-Path Zero Latency:** Voice commands matching common computer actions (windows, tabs, shortcuts, volume, arithmetic, date/time) execute in **under 20 milliseconds**, completely bypassing LLM round-trips.
2. **GPU Acceleration on RTX 5050 (Blackwell):** PyTorch `2.7.1+cu128` successfully unlocks the RTX 5050 Laptop GPU for both `faster-whisper` and `MeloTTS`. TTS synthesis time is **~60 ms per sentence** (Real-Time Factor ~0.04), meaning speech generation is nearly 25× faster than playback.
3. **Speculative STT Overlap:** Because final transcription starts at the pause boundary, the 342 ms transcription overlaps with the user's 380–750 ms end-of-speech silence window, making turnaround feel instantaneous to the human ear.
4. **FreeLLMAPI Performance:** FreeLLMAPI responds with a median TTFT of **1.09 seconds**, providing fluid conversational replies when network intelligence is required.
