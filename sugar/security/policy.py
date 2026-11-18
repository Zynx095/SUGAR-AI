"""The central permission engine: (tool, arguments, provenance) → allow / confirm / deny.

Rules (in this order):
  1. Credential access is denied — to everyone, including the model acting on a direct request.
  2. Untrusted provenance (the model has read web pages, files, search results, tool output…):
       read-only → allow; low-risk or modifying → confirm, telling the user where it came from;
       anything else (unknown, network transfer, destructive, privileged) → denied outright.
     A web page saying "run this to fix your PC" can never become an executed command.
  3. Direct user command (typed/spoken, matched by the grammar): runs up to modifications;
     unknown, network transfer, destructive and privileged actions are confirmed.
  4. The model acting on a direct request (untainted): read-only and low-risk run; modifications run
     only when ``permissions.auto_approve_level`` ≥ 2; everything riskier is confirmed.
     "Do everything" never pre-authorises a dangerous command.
  5. ``permissions.trusted_tools`` may skip confirmation for actions up to MODIFICATION only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sugar.security.commands import Risk, classify_command, is_credential_path
from sugar.security.provenance import Provenance


class Decision(StrEnum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    risk: Risk
    reason: str


_LEVEL_TO_RISK = {0: Risk.READ_ONLY, 1: Risk.LOW_RISK_REVERSIBLE, 2: Risk.MODIFICATION, 3: Risk.DESTRUCTIVE}
_PATH_ARGS = ("path", "source", "destination", "file", "cwd")


def action_risk(tool: Any, args: dict[str, Any]) -> tuple[Risk, str]:
    """The risk of one concrete action (tool + arguments)."""
    if tool.name == "terminal.run":
        command = classify_command(str(args.get("command", "")))
        if command.risk < Risk.CREDENTIAL_ACCESS and is_credential_path(str(args.get("cwd") or "")):
            return Risk.CREDENTIAL_ACCESS, "runs inside a credentials location"
        return command.risk, command.reason
    for key in _PATH_ARGS:
        if args.get(key) and is_credential_path(str(args[key])):
            return Risk.CREDENTIAL_ACCESS, f"touches a credentials file ({args[key]})"
    level = int(tool.level_for(args))
    risk = _LEVEL_TO_RISK.get(level, Risk.DESTRUCTIVE)
    return risk, f"{tool.name} is {risk.label.replace('_', ' ')}"


def decide(tool: Any, args: dict[str, Any], provenance: Provenance, *, auto_approve_level: int = 1,
           trusted_tools: tuple[str, ...] | list[str] = ()) -> Verdict:
    risk, why = action_risk(tool, args)
    if risk == Risk.CREDENTIAL_ACCESS:
        return Verdict(Decision.DENY, risk, f"credential access is restricted: {why}")

    if provenance.tainted:
        if risk == Risk.READ_ONLY:
            return Verdict(Decision.ALLOW, risk, f"read-only ({why})")
        if risk <= Risk.MODIFICATION:
            return Verdict(Decision.CONFIRM, risk, f"{why}; proposed after reading untrusted content")
        return Verdict(Decision.DENY, risk,
                       f"{why}; the instruction came from untrusted content ({provenance.describe()})")

    trusted = tool.name in trusted_tools and risk <= Risk.MODIFICATION
    if provenance.direct:
        if risk <= Risk.MODIFICATION or trusted:
            return Verdict(Decision.ALLOW, risk, f"direct request ({why})")
        return Verdict(Decision.CONFIRM, risk, why)

    # the model, acting on the user's request, with no untrusted content in the turn
    if risk == Risk.READ_ONLY or trusted:
        return Verdict(Decision.ALLOW, risk, why)
    if risk == Risk.LOW_RISK_REVERSIBLE:
        decision = Decision.ALLOW if auto_approve_level >= 1 else Decision.CONFIRM
        return Verdict(decision, risk, why)
    if risk == Risk.MODIFICATION:
        decision = Decision.ALLOW if auto_approve_level >= 2 else Decision.CONFIRM
        return Verdict(decision, risk, why)
    return Verdict(Decision.CONFIRM, risk, why)
