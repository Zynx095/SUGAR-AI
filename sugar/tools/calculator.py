"""Spoken arithmetic, evaluated safely (AST whitelist — never ``eval``)."""

from __future__ import annotations

import ast
import math
import operator
import re

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALES = {"hundred": 100, "thousand": 1_000, "lakh": 100_000, "million": 1_000_000, "crore": 10_000_000,
           "billion": 1_000_000_000}

_PHRASES = [
    (r"\bsquare root of\b", " sqrt "),
    (r"\bcube root of\b", " cbrt "),
    (r"\b(?:to the power of|raised to(?: the power of)?|to the)\b", " ** "),
    (r"\bsquared\b", " ** 2 "),
    (r"\bcubed\b", " ** 3 "),
    (r"\bpercent of\b", " / 100 * "),
    (r"\b(?:percent|per cent)\b", " / 100 "),
    (r"\b(?:multiplied by|times|into)\b", " * "),
    (r"\b(?:divided by|over)\b", " / "),
    (r"\bplus\b|\badd\b", " + "),
    (r"\bminus\b|\bsubtract\b", " - "),
    (r"\bmod(?:ulo)?\b", " % "),
    (r"[×x](?=\s*\d)", " * "),
    (r"÷", " / "),
    (r"\^", " ** "),
]

_BINARY = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCTIONS = {"sqrt": math.sqrt, "cbrt": lambda x: math.copysign(abs(x) ** (1 / 3), x)}


def _words_to_numbers(text: str) -> str:
    """'three hundred and twenty five point five' → '325.5'."""
    tokens = text.split()
    out: list[str] = []
    current: float | None = None
    total = 0.0
    decimal: list[str] | None = None

    def flush() -> None:
        nonlocal current, total, decimal
        if current is not None or total:
            value = total + (current or 0)
            number = str(int(value)) if float(value).is_integer() else str(value)
            if decimal:
                number += "." + "".join(decimal)
            out.append(number)
        current, total, decimal = None, 0.0, None

    for token in tokens:
        word = token.strip(",")
        if decimal is not None and word in _UNITS and _UNITS[word] < 10:
            decimal.append(str(_UNITS[word]))
            continue
        if word in _UNITS:
            current = (current or 0) + _UNITS[word]
        elif word in _SCALES and (current is not None or total):
            scale = _SCALES[word]
            if scale == 100:
                current = (current or 1) * 100
            else:
                total += (current or 1) * scale
                current = None
        elif word == "and" and (current is not None or total):
            continue
        elif word == "point" and (current is not None or total):
            decimal = []
        else:
            flush()
            out.append(token)
    flush()
    return " ".join(out)


def _evaluate(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left, right = _evaluate(node.left), _evaluate(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("exponent too large")
        return _BINARY[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_evaluate(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS and len(node.args) == 1:
        return _FUNCTIONS[node.func.id](_evaluate(node.args[0]))
    raise ValueError("unsupported expression")


def evaluate_spoken_math(text: str) -> float | None:
    """Evaluate arithmetic said out loud; None if it isn't clearly arithmetic."""
    expression = text.lower().strip().rstrip("?.! ")
    expression = re.sub(r"^(?:what(?:'s| is)|calculate|compute|how much is|solve|evaluate)\s+", "", expression)
    expression = _words_to_numbers(expression)
    for pattern, replacement in _PHRASES:
        expression = re.sub(pattern, replacement, expression)
    expression = re.sub(r"(?<=\d),(?=\d{3})", "", expression)
    expression = re.sub(r"\bsqrt\s+([\d.]+)", r"sqrt(\1)", expression)
    expression = re.sub(r"\bcbrt\s+([\d.]+)", r"cbrt(\1)", expression)
    expression = re.sub(r"\s+", " ", expression).strip()
    if not expression or not re.fullmatch(r"[\d\s.+\-*/%()a-z]*", expression):
        return None
    if not re.search(r"\d", expression) or not re.search(r"[+\-*/%]|sqrt|cbrt", expression):
        return None
    if re.search(r"[a-z]", re.sub(r"sqrt|cbrt", "", expression)):
        return None
    try:
        value = _evaluate(ast.parse(expression, mode="eval"))
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError, TypeError):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def format_number(value: float) -> str:
    if float(value).is_integer() and abs(value) < 1e15:
        return f"{int(value):,}"
    return f"{value:,.4f}".rstrip("0").rstrip(".")
