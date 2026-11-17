# SID — Storage and Offline Report

Measured 18 Nov 2026. Rule followed throughout: models and large dependencies go to **D:**, nothing new
to C:, and nothing that might be user data, model data, source, configuration or logs is deleted
automatically.

## Where things live

| What | Location | Size |
|---|---|---|
| Repository, venv, `data/` (SQLite memory, telemetry, overrides, logs, voice samples) | `D:\College\SOMESHIT DOWNLOADS\SUGAR-AI` | — |
| Whisper models (small.en, large-v3-turbo) | `D:\Sugar_Models\Whisper` | 2.2 GB |
| Ollama 0.40.16 portable binary + `qwen2.5:3b` | `D:\Sugar_Models\ollama` (models in `…\ollama\models`, 1.8 GB) | 4.0 GB |
| Hugging Face cache (MeloTTS English, BERT) | `D:\Sugar_Models\hf` | 1.3 GB |
| OpenVoice V2 converter | `D:\Sugar_Models\OpenVoiceV2` | 126 MB |
| pip cache (`PIP_CACHE_DIR`) | `D:\pipcache` | 0.45 GB |
| Temporary audio and test files (`TMP`/`TEMP` for runs) | `D:\Sugar_Models\tmp` | 189 MB |
| Downloaded Ollama archive (already extracted) | `D:\Sugar_Models\downloads\ollama-windows-amd64.zip` | 1.6 GB |

`main.py` now sets `HF_HOME=D:\Sugar_Models\hf` when that folder exists, so MeloTTS and BERT load
and download there (verified: offline load from `D:\Sugar_Models\hf\hub`). Ollama's models directory
is passed explicitly as `OLLAMA_MODELS`, so a pull can't land in `%USERPROFILE%\.ollama`.

## C: drive

Free space: **2.86 GB** (it was 0.58 GB before the earlier clean-up of disposable package caches, and
3.8 GB right after it; I did not trace the drop since then (Windows and other programs also write to C:). SID itself writes no
models, caches or data to C:; the only C: writes during its runs are the Claude Code CLI's own small
session logs in `%USERPROFILE%\.claude`).

What SID-related still sits on C: and why:

| Item | Size | Status |
|---|---|---|
| `%USERPROFILE%\.cache\huggingface` | 0.60 GB | **Duplicate** of `D:\Sugar_Models\hf` (same MeloTTS and BERT snapshot ids and file lists). No longer used by SID now that `HF_HOME` points to D:. Not deleted: it is model data, and other programs on this PC could use it. |
| Python 3.12 install, Claude Code CLI | — | pre-existing system installs, required |
| pip cache on C: | none | pip is configured to use `D:\pipcache` |

Reclaimable if you choose (run yourself; I did not):

```powershell
# 0.6 GB on C: — only if nothing else on this PC uses the default Hugging Face cache
Remove-Item -Recurse -Force "$env:USERPROFILE\.cache\huggingface"
# 1.6 GB on D: — the Ollama zip, already extracted (D: has 318 GB free, so there's no pressure)
Remove-Item "D:\Sugar_Models\downloads\ollama-windows-amd64.zip"
```

## Offline operation — VERIFIED

Test: real app, `SID__LLM__FREELLM__ENABLED=false` and `SID__LLM__CLAUDE__ENABLED=false`, Whisper and
MeloTTS with `HF_HUB_OFFLINE=1`.

| Step | Result |
|---|---|
| Start | SID started its own `ollama serve` from D: (`started`), connectivity `local_only` |
| "Explain how black holes work in two sentences." | answered by `ollama` in 2.6 s (first token 2266 ms, cold) |
| "Write a four line poem about rain." | answered by `ollama` in 0.8 s (first token 304 ms) |
| "Modify my portfolio." | no model needed: selected `yukith-portfolio`, asked what to change |
| Shutdown | Ollama process tree stopped (`stopped`), no `ollama.exe` left running |

What does **not** work offline: research and web lookups (need the internet), YouTube/Spotify search
results, Claude Code (needs Anthropic's service), and anything a 3B model can't answer well.

## Disk-related settings

- `paths.models_dir` (default `D:\Sugar_Models`), `llm.ollama.executable`, `llm.ollama.models_dir`,
  `llm.ollama.manage_server`.
- `telemetry.retention_days` (30) bounds `data/telemetry.db`; logs rotate in `data/logs`.
