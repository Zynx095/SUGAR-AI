"""
Sugar AI — Multi-model intelligence layer.

Responsibilities:
- semantic intent routing
- specialist model selection
- conversation construction
- Ollama communication
- graceful fallback
"""

import json
import re
from dataclasses import dataclass
from typing import Literal

import ollama

from config import (
    ROSTER,
    MODEL_SETTINGS,
    BASE_SYSTEM_PROMPT,
    SPECIALIST_PROMPTS,
    DEFAULT_ROUTE,
    OLLAMA_KEEP_ALIVE,
)


Route = Literal[
    "fast",
    "general",
    "reasoning",
    "math",
    "coding",
    "medical",
]


VALID_ROUTES = {
    "fast",
    "general",
    "reasoning",
    "math",
    "coding",
    "medical",
}


@dataclass
class RouteDecision:
    route: str
    confidence: float = 0.0
    complexity: str = "normal"
    reason: str = ""


# ============================================================
# ROUTER PROMPT
# ============================================================

ROUTER_PROMPT = """
You are the routing system for a local AI assistant.

Classify the user's request into EXACTLY ONE category.

CATEGORIES

fast:
Very simple questions, transformations, classification,
short summaries or lightweight tasks.

general:
Normal conversation, general knowledge, explanations,
history, science, study questions, recommendations and writing.

coding:
Programming, debugging, software engineering, Git,
algorithms, APIs, databases, operating systems,
networking code, web development or computer architecture.

math:
Mathematics requiring actual mathematical reasoning,
equations, calculus, probability, statistics, algebra,
geometry or mathematical proofs.

medical:
Health, medicine, symptoms, anatomy, medications,
disease, nutrition in a medical context or biological health.

reasoning:
Difficult logical reasoning, multi-step analysis,
planning, puzzles or technical reasoning that does not
fit coding/math/medical.

Return ONLY JSON.

Schema:

{
    "route": "general",
    "confidence": 0.95,
    "complexity": "normal",
    "reason": "short explanation"
}

route MUST be one of:

fast
general
coding
math
medical
reasoning

complexity MUST be:

simple
normal
high

Do not answer the user's question.
Only classify it.
""".strip()


# ============================================================
# SIMPLE HEURISTICS
# ============================================================

def _obvious_route(text: str) -> str | None:
    """
    Handle extremely obvious requests without waking the router.

    This is deliberately conservative.
    Semantic routing handles ambiguous requests.
    """

    p = text.lower().strip()

    coding_markers = (
        "traceback",
        "syntaxerror",
        "typeerror",
        "npm error",
        "pip install",
        "git commit",
        "git push",
        "dockerfile",
    )

    if any(marker in p for marker in coding_markers):
        return "coding"

    return None


# ============================================================
# JSON EXTRACTION
# ============================================================

def _extract_json(text: str) -> dict:
    text = text.strip()

    # Remove markdown fences if the router ignored instructions.
    text = re.sub(r"^```(?:json)?", "", text, flags=re.I).strip()
    text = re.sub(r"```$", "", text).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Attempt to recover the first JSON object.
    match = re.search(r"\{.*?\}", text, flags=re.S)

    if not match:
        raise ValueError("Router did not return JSON.")

    return json.loads(match.group(0))


# ============================================================
# ROUTING
# ============================================================

def route_model(user_input: str) -> RouteDecision:
    """
    Determine which Sugar specialist should handle the request.
    """

    user_input = user_input.strip()

    if not user_input:
        return RouteDecision(
            route=DEFAULT_ROUTE,
            confidence=1.0,
            complexity="simple",
            reason="Empty request fallback",
        )

    obvious = _obvious_route(user_input)

    if obvious:
        return RouteDecision(
            route=obvious,
            confidence=1.0,
            complexity="normal",
            reason="Deterministic routing",
        )

    try:
        response = ollama.chat(
            model=ROSTER["router"],
            messages=[
                {
                    "role": "system",
                    "content": ROUTER_PROMPT,
                },
                {
                    "role": "user",
                    "content": user_input,
                },
            ],
            options=MODEL_SETTINGS["router"],
            keep_alive=OLLAMA_KEEP_ALIVE,
        )

        raw = response["message"]["content"]

        data = _extract_json(raw)

        route = str(data.get("route", DEFAULT_ROUTE)).lower()

        if route not in VALID_ROUTES:
            route = DEFAULT_ROUTE

        try:
            confidence = float(data.get("confidence", 0.5))
        except (ValueError, TypeError):
            confidence = 0.5

        confidence = max(0.0, min(confidence, 1.0))

        complexity = str(
            data.get("complexity", "normal")
        ).lower()

        if complexity not in {"simple", "normal", "high"}:
            complexity = "normal"

        return RouteDecision(
            route=route,
            confidence=confidence,
            complexity=complexity,
            reason=str(data.get("reason", "")),
        )

    except Exception as exc:
        return RouteDecision(
            route=DEFAULT_ROUTE,
            confidence=0.0,
            complexity="normal",
            reason=f"Router fallback: {exc}",
        )


# ============================================================
# SYSTEM PROMPTS
# ============================================================

def build_system_prompt(route: str) -> str:
    specialist = SPECIALIST_PROMPTS.get(
        route,
        SPECIALIST_PROMPTS["general"],
    )

    return (
        BASE_SYSTEM_PROMPT
        + "\n\nSPECIALIST ROLE:\n"
        + specialist.strip()
    )


# ============================================================
# MODEL EXECUTION
# ============================================================

def call_ollama(
    prompt: str,
    history: list[dict] | None,
    model_key: str,
) -> str:

    route = (
        model_key
        if model_key in VALID_ROUTES
        else DEFAULT_ROUTE
    )

    actual_model = ROSTER.get(
        route,
        ROSTER[DEFAULT_ROUTE],
    )

    options = MODEL_SETTINGS.get(
        route,
        MODEL_SETTINGS[DEFAULT_ROUTE],
    )

    messages = [
        {
            "role": "system",
            "content": build_system_prompt(route),
        }
    ]

    if history:
        for message in history:
            role = message.get("role")
            content = message.get("content")

            if role not in {"user", "assistant"}:
                continue

            if not content:
                continue

            messages.append({
                "role": role,
                "content": str(content),
            })

    messages.append({
        "role": "user",
        "content": prompt,
    })

    try:
        response = ollama.chat(
            model=actual_model,
            messages=messages,
            options=options,
            keep_alive=OLLAMA_KEEP_ALIVE,
        )

        content = response["message"]["content"]

        if not content:
            return "I couldn't generate a response."

        return content.strip()

    except Exception as exc:
        return (
            "I couldn't reach my local model. "
            f"Model: {actual_model}. Error: {exc}"
        )