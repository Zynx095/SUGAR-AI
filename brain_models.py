"""
Sugar AI — Multi-model intelligence layer.

Responsibilities:
- semantic intent routing
- specialist model selection
- conversation construction
- Ollama communication with strict timeouts
- parsing DeepSeek <think> tags
"""

import json
import re
from dataclasses import dataclass
from typing import Literal, Tuple, List, Dict, Optional

from ollama import Client

from config import (
    ROSTER,
    MODEL_SETTINGS,
    BASE_SYSTEM_PROMPT,
    SPECIALIST_PROMPTS,
    DEFAULT_ROUTE,
    OLLAMA_KEEP_ALIVE,
    OLLAMA_HOST,
    OLLAMA_TIMEOUT
)

Route = Literal["fast", "general", "reasoning", "math", "coding", "medical"]
VALID_ROUTES = {"fast", "general", "reasoning", "math", "coding", "medical"}

# Use an explicit client so we can enforce timeouts and prevent UI hangs.
ollama_client = Client(host=OLLAMA_HOST, timeout=OLLAMA_TIMEOUT)

@dataclass
class RouteDecision:
    route: str
    confidence: float = 0.0
    complexity: str = "normal"
    reason: str = ""

ROUTER_PROMPT = """
You are the routing system for a local AI assistant.
Classify the user's request into EXACTLY ONE category.

CATEGORIES:
fast: Simple questions, short summaries, trivial facts.
general: Normal conversation, general knowledge, recommendations.
coding: Programming, debugging, software architecture, OS commands.
math: Mathematical reasoning, equations, probability, proofs.
medical: Health, symptoms, anatomy, biology, nutrition.
reasoning: Difficult logical puzzles, multi-step analysis.

Return ONLY JSON. No markdown formatting.
Schema:
{"route": "general", "confidence": 0.95, "complexity": "normal", "reason": "brief reason"}
""".strip()

def _obvious_route(text: str) -> Optional[str]:
    """Handle deterministic requests to bypass the LLM router."""
    p = text.lower().strip()
    coding_markers = ("traceback", "syntaxerror", "typeerror", "pip install", "dockerfile")
    if any(m in p for m in coding_markers):
        return "coding"
    return None

def _extract_json(text: str) -> dict:
    text = text.strip()
    # Strip deepseek reasoning if router model hallucinated it
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    
    match = re.search(r"\{.*?\}", text, flags=re.S)
    if not match:
        raise ValueError("Router did not return valid JSON.")
    return json.loads(match.group(0))

def parse_model_response(raw_text: str) -> Tuple[str, str]:
    """
    Separates DeepSeek <think> reasoning blocks from the actual output.
    Returns: (reasoning_text, spoken_text)
    """
    think_match = re.search(r"<think>(.*?)</think>", raw_text, flags=re.DOTALL)
    if think_match:
        reasoning = think_match.group(1).strip()
        spoken = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip()
        return reasoning, spoken
    return "", raw_text.strip()

def route_model(user_input: str) -> RouteDecision:
    """Determines which Sugar specialist handles the request."""
    user_input = user_input.strip()
    if not user_input:
        return RouteDecision(route=DEFAULT_ROUTE, confidence=1.0, reason="Empty request")

    obvious = _obvious_route(user_input)
    if obvious:
        return RouteDecision(route=obvious, confidence=1.0, reason="Deterministic keyword")

    try:
        response = ollama_client.chat(
            model=ROSTER["router"],
            messages=[
                {"role": "system", "content": ROUTER_PROMPT},
                {"role": "user", "content": user_input},
            ],
            options=MODEL_SETTINGS["router"],
            keep_alive=OLLAMA_KEEP_ALIVE,
        )

        data = _extract_json(response["message"]["content"])
        route = str(data.get("route", DEFAULT_ROUTE)).lower()
        if route not in VALID_ROUTES:
            route = DEFAULT_ROUTE

        return RouteDecision(
            route=route,
            confidence=max(0.0, min(float(data.get("confidence", 0.5)), 1.0)),
            complexity=str(data.get("complexity", "normal")).lower(),
            reason=str(data.get("reason", "")),
        )

    except Exception as exc:
        return RouteDecision(route=DEFAULT_ROUTE, reason=f"Router fallback: {exc}")

def build_system_prompt(route: str) -> str:
    specialist = SPECIALIST_PROMPTS.get(route, SPECIALIST_PROMPTS["general"])
    return f"{BASE_SYSTEM_PROMPT}\n\nSPECIALIST ROLE:\n{specialist.strip()}"

def call_ollama(prompt: str, history: List[Dict[str, str]], model_key: str) -> str:
    route = model_key if model_key in VALID_ROUTES else DEFAULT_ROUTE
    actual_model = ROSTER.get(route, ROSTER[DEFAULT_ROUTE])
    options = MODEL_SETTINGS.get(route, MODEL_SETTINGS[DEFAULT_ROUTE])

    messages = [{"role": "system", "content": build_system_prompt(route)}]
    
    if history:
        for msg in history:
            if msg.get("role") in {"user", "assistant"} and msg.get("content"):
                messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({"role": "user", "content": prompt})

    try:
        response = ollama_client.chat(
            model=actual_model,
            messages=messages,
            options=options,
            keep_alive=OLLAMA_KEEP_ALIVE,
        )
        content = response["message"]["content"]
        return content.strip() if content else "I couldn't generate a response."
    except Exception as exc:
        raise RuntimeError(f"Ollama connection error on {actual_model}: {exc}")