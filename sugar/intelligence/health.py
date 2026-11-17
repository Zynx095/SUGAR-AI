"""Health and medical requests: emergency triage, private-data detection and the answering policy.

SID explains medical terms, lab values, conditions and medications for education, and helps prepare
questions for a doctor. It does not diagnose, prescribe or tell anyone to change prescribed treatment.
Possible emergencies get a short, direct instruction to seek help now — before any long answer.

Personal health information (the user's own symptoms, results, medications, records) is sensitive:
it is answered by a local model when one is running, and sent to an online model only after the
user allows it (``health.allow_external`` or, for this session, "allow online health answers").
"""

from __future__ import annotations

import re

EMERGENCY_NUMBER = "112"  # India's emergency number (the user's region); 108 for ambulance also works

_EMERGENCY = re.compile(
    r"(?i)\b(?:chest pain|pain in (?:my|the) chest|can'?t breathe|cannot breathe|can'?t breath|trouble breathing|"
    r"short(?:ness)? of breath|choking|unconscious|passed out|not breathing|unresponsive|seizure|convulsing|"
    r"stroke|face (?:is )?drooping|slurred speech|one side (?:of (?:my|his|her) body )?(?:is )?(?:numb|weak)|"
    r"severe bleeding|bleeding (?:a lot|heavily|won'?t stop)|overdose|overdosed|took too many (?:pills|tablets)|"
    r"poison(?:ed|ing)|anaphyla|throat (?:is )?(?:closing|swelling)|suicid|kill (?:myself|me)|end my life|"
    r"want to die|self[- ]harm|hurt myself|heart attack|coughing (?:up )?blood|vomiting blood|"
    r"severe (?:head|abdominal|stomach) pain|worst headache)\b"
)
_SELF_HARM = re.compile(r"(?i)\b(?:suicid|kill (?:myself|me)|end my life|want to die|self[- ]harm|hurt myself)\b")

_HEALTH_TERMS = re.compile(
    r"(?i)\b(?:health|medical|medicine|medication|medicines|drug interaction|dose|dosage|mg\b|tablet|pill|"
    r"prescription|prescribed|side effects?|symptoms?|diagnos\w*|disease|condition|infection|fever|cough|cold|flu|"
    r"pain|ache|headache|migraine|rash|allerg\w*|asthma|diabetes|insulin|blood (?:test|pressure|sugar|report|work)|"
    r"cholesterol|haemoglobin|hemoglobin|hba1c|platelets?|wbc|rbc|thyroid|tsh|vitamin|deficien\w*|anemi\w*|"
    r"anaemi\w*|cancer|tumou?r|heart|kidney|liver|lungs?|surgery|therapy|doctor|physician|clinic|hospital|"
    r"lab (?:result|report|value)s?|x-?ray|mri|ct scan|ecg|ekg|pregnan\w*|period|menstrua\w*|mental health|anxiety|"
    r"depression|insomnia|antibiotic|paracetamol|ibuprofen|antidepressant|vaccine|virus|bacteria|nutrition|"
    r"calories|bmi|injury|fracture|sprain|wound)\b"
)
_PERSONAL = re.compile(
    r"(?i)\b(?:i have|i've had|i'?m having|i am having|i feel|i'?ve been feeling|my (?:\w+ )?(?:test|results?|report|"
    r"levels?|blood|doctor|prescription|medication|meds|symptoms?|pain|diagnosis|scan|condition|heart|"
    r"pressure|sugar)|i'?m (?:on|taking)|i took|i was diagnosed|i got diagnosed|mine (?:is|are))\b"
)
_LAB_VALUE = re.compile(r"(?i)\b\d+(?:\.\d+)?\s*(?:mg/dl|mmol/l|g/dl|mmhg|iu/l|u/l|miu/l|%|bpm|ng/ml|pg/ml)\b")


def is_health_request(text: str) -> bool:
    return bool(_HEALTH_TERMS.search(text)) or bool(_EMERGENCY.search(text))


def emergency_message(text: str) -> str | None:
    """A short emergency instruction, or None. Deliberately not left to a model."""
    if not _EMERGENCY.search(text):
        return None
    if _SELF_HARM.search(text):
        return ("I'm glad you told me. Please reach out right now: call Tele-MANAS on 14416, or 112 if you are in "
                "immediate danger. If you can, tell someone near you and stay with them. I'm here to keep talking.")
    return (f"This could be a medical emergency. Call {EMERGENCY_NUMBER} now, or have someone take you to the "
            "nearest emergency department. Don't wait for symptoms to pass, and don't drive yourself.")


def contains_personal_health_data(text: str) -> bool:
    return is_health_request(text) and (bool(_PERSONAL.search(text)) or bool(_LAB_VALUE.search(text)))


HEALTH_INSTRUCTIONS = """This is a health question. Answer as a careful health educator, not a doctor.
- Explain clearly what the term, measurement, condition or medication is and what it generally means.
- For lab values, give the typical reference ranges and say that ranges vary by laboratory, age and sex.
- Never diagnose, never prescribe, and never suggest starting, stopping or changing a prescribed treatment.
  Say plainly when something needs a clinician to interpret, and suggest questions the user could ask.
- Separate well-established facts from uncertainty. Never invent statistics, studies or references.
- If anything described could be serious, say so and recommend seeing a doctor promptly.
- Keep the spoken part short; put detail in markdown."""
