"""
Sugar AI — Central Configuration

Local-first multi-model AI assistant optimized for an RTX 5050 (8GB VRAM).
Core AI functionality works entirely through local Ollama models.
"""

from pathlib import Path

# ============================================================
# PATHS
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
CHROMA_DB_DIR = BASE_DIR / "chroma_db"
CHAT_LOG_FILE = BASE_DIR / "Sugar_Chat_Logs.txt"
WHISPER_MODEL_DIR = Path(r"D:\Sugar_Models\Whisper")

# ============================================================
# MODELS ROSTER
# ============================================================
ROSTER = {
    "router": "gemma3:1b",
    "fast": "phi4-mini",
    "general": "gemma3:4b",
    "reasoning": "deepseek-r1:8b",
    "math": "deepseek-r1:8b",
    "coding": "deepseek-r1:8b",
    "medical": "medgemma:4b",
}

DEFAULT_ROUTE = "general"

# ============================================================
# OLLAMA NETWORK & LIFECYCLE
# ============================================================
OLLAMA_HOST = "http://localhost:11434"
OLLAMA_TIMEOUT = 60.0  # Seconds before giving up on a stalled request
OLLAMA_KEEP_ALIVE = "5m"  # Unload large specialists after 5 mins to save VRAM

# ============================================================
# MODEL SETTINGS
# ============================================================
MODEL_SETTINGS = {
    "router":    {"temperature": 0.0, "num_ctx": 2048},
    "fast":      {"temperature": 0.3, "num_ctx": 4096},
    "general":   {"temperature": 0.6, "num_ctx": 8192},
    "reasoning": {"temperature": 0.2, "num_ctx": 16384},
    "math":      {"temperature": 0.1, "num_ctx": 16384},
    "coding":    {"temperature": 0.1, "num_ctx": 16384},
    "medical":   {"temperature": 0.2, "num_ctx": 8192},
}

# ============================================================
# SUGAR PERSONALITY
# ============================================================
BASE_SYSTEM_PROMPT = """
You are Sugar, a fast local AI desktop assistant.

Your priorities are:
1. Correctness and clear reasoning.
2. Concise conversational responses.
3. Never pretend you performed an action you did not perform.
4. If uncertain, explicitly say so.

The user's name is Yuki. Do not repeatedly greet the user.
Do not unnecessarily mention that you are an AI.
""".strip()

SPECIALIST_PROMPTS = {
    "general": "Handle everyday questions, explanations, conversation, summaries, and general knowledge.",
    "fast": "Handle lightweight requests quickly. Prefer short, direct answers.",
    "coding": "Produce correct, runnable code. Diagnose errors before rewriting. Preserve existing architecture. Explain technical decisions concisely.",
    "math": "Prioritize mathematical correctness. Work through non-trivial calculations step-by-step.",
    "reasoning": "Break complicated problems into logical components. Check assumptions. Prioritize correctness over speed.",
    "medical": "Provide careful educational medical information. Distinguish established facts from uncertainty. Do not claim to diagnose.",
}

# ============================================================
# VOICE
# ============================================================
WHISPER_MODEL = "large-v3-turbo"
DEFAULT_SENSITIVITY = 0.3
DEFAULT_TTS_SPEED = 1.1
DEFAULT_VOICE = "EN-Newest"
HISTORY_WINDOW = 8