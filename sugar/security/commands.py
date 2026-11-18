"""Risk classification for PowerShell / shell commands.

Conservative by construction: dangerous keywords are searched *anywhere* in the command (inside
pipelines, script blocks and sub-expressions), and anything that isn't recognised as harmless is
UNKNOWN, which is never auto-approved. The result is the highest risk found.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum


class Risk(IntEnum):
    READ_ONLY = 0
    LOW_RISK_REVERSIBLE = 1
    MODIFICATION = 2
    UNKNOWN = 3
    NETWORK_OR_EXTERNAL_TRANSFER = 4
    DESTRUCTIVE = 5
    PRIVILEGED = 6
    CREDENTIAL_ACCESS = 7

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass(frozen=True)
class CommandRisk:
    risk: Risk
    reason: str


def _any(*words: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w-])(?:" + "|".join(words) + r")(?![\w-])", re.IGNORECASE)


# Searched anywhere in the command, highest risk first.
_CREDENTIAL = re.compile(
    r"(?i)get-credential|cmdkey|convertfrom-securestring|\bvaultcmd\b|mimikatz|lsass|sekurlsa|"
    r"\$env:[\w]*(?:key|token|secret|pass|pwd|credential|auth)[\w]*|"
    r"(?:get-childitem|gci|dir|ls|get-item)\s+env:|getenvironmentvariables?\(|"
    r"(?:^|[\\/\s'\"])\.env(?:\.[\w]+)?(?:['\"\s]|$)|id_rsa|id_ed25519|\.pem\b|\.pfx\b|\.p12\b|"
    r"\.git-credentials|\.npmrc|\.pypirc|credentials\.json|login data|\\cookies\b|\\sam\b|\\security\\|"
    r"ntds\.dit|keychain|\.kdbx\b"
)
_PRIVILEGED = re.compile(
    r"(?i)-verb\s+runas|\brunas\b|set-executionpolicy|\breg(?:\.exe)?\s+(?:add|delete|import|load|restore)|"
    r"(?:new|set|remove)-itemproperty\s+[^|;]*hk(?:lm|cu|cr|u|cc)|\bhk(?:lm|cu):|\bbcdedit\b|"
    r"\bsc(?:\.exe)?\s+(?:config|create|delete|stop|start)\b|(?:set|new|remove)-service|\bnetsh\b|"
    r"(?:set|new|remove)-netfirewall|(?:add|set|remove)-mppreference|disable-windowsoptionalfeature|"
    r"enable-windowsoptionalfeature|\bdism\b|\bschtasks\b|register-scheduledtask|\bicacls\b|\btakeown\b|"
    r"\bwinget\b|\bchoco\b|install-module|install-package|\bshutdown\b|restart-computer|stop-computer|"
    r"wevtutil\s+cl|clear-eventlog|\bdiskpart\b|\bformat(?:\.com)?\s+[a-z]:|format-volume|"
    r"invoke-expression|\biex\b|-enc(?:odedcommand)?\b|frombase64string|downloadstring|add-type|"
    r"set-mpp|uninstall-|set-itemproperty|new-psdrive\s+[^|;]*hk"
)
_DESTRUCTIVE = re.compile(
    r"(?i)(?<![\w-])(?:remove-item|rm|ri|del|erase|rd|rmdir|clear-content|clear-recyclebin|remove-[a-z]+)"
    r"(?![\w-])|\bgit\s+clean\b|\bgit\s+reset\s+--hard|\bgit\s+push\s+(?:-f|--force)|\bcipher\s+/w|"
    r"\bgit\s+branch\s+-D\b|\bdrop\s+(?:table|database)\b|\btruncate\s+table\b|stop-process|\bkill\b|"
    r"\btaskkill\b"
)
_NETWORK = re.compile(
    r"(?i)(?<![\w-])(?:invoke-webrequest|iwr|invoke-restmethod|irm|curl|wget|start-bitstransfer|scp|sftp|ftp|"
    r"ssh|send-mailmessage|net\.webclient|webclient|httpclient|tftp|nc|ncat)(?![\w-])|"
    r"\bgit\s+(?:push|pull|clone|fetch)\b|\bnpm\s+publish\b|\bpip\s+download\b|\btwine\s+upload\b"
)
_REDIRECT = re.compile(r"(?<![\d$])>{1,2}(?!\s*\$null)|\|\s*(?:set-content|add-content|out-file|tee-object)",
                       re.IGNORECASE)

# Leading commands of each pipeline segment.
_READ_ONLY_LEAD = re.compile(
    r"(?i)^(?:get-[a-z]+|test-[a-z]+|measure-[a-z]+|select-[a-z]+|where-object|where|sort-object|sort|"
    r"group-object|format-[a-z]+|ft|fl|out-string|out-host|out-null|write-output|write-host|echo|dir|ls|gci|"
    r"cat|type|gc|pwd|whoami|hostname|systeminfo|ipconfig|tasklist|resolve-path|split-path|join-path|"
    r"convertto-json|convertfrom-json|convertto-csv|compare-object|foreach-object|%|\?|nvidia-smi|ver|"
    r"git\s+(?:status|log|diff|show|branch|remote\s+-v|rev-parse|ls-files|blame)|"
    r"(?:python|py|node|npm|pip|git|dotnet|java|go|cargo|rustc)\s+(?:--version|-v|-V|version)|"
    r"pip\s+(?:list|show|freeze)|npm\s+(?:ls|list|view|outdated)|wmic\s+\w+\s+get)\b"
)
_LOW_RISK_LEAD = re.compile(
    r"(?i)^(?:mkdir|md|new-item\s+[^|;]*-itemtype\s+directory|set-location|cd|push-location|pop-location|"
    r"copy-item|cp|copy|git\s+(?:add|stash|switch|checkout\s+-b)|pytest|python\s+-m\s+pytest|py\s+-m\s+pytest|"
    r"npm\s+(?:test|run\s+(?:test|lint|build))|yarn\s+(?:test|build)|pnpm\s+(?:test|build)|cargo\s+(?:test|build)|"
    r"go\s+(?:test|build)|dotnet\s+(?:test|build)|ruff|eslint|tsc|mvn\s+test|gradle\s+test|start\s+https?://|"
    r"start-process\s+['\"]?https?://)"
)
_MODIFICATION_LEAD = re.compile(
    r"(?i)^(?:set-content|add-content|out-file|new-item|rename-item|ren|move-item|mv|move|expand-archive|"
    r"compress-archive|git\s+\w+|pip\s+install|python\s+-m\s+pip\s+install|npm\s+(?:install|i|ci|uninstall|run)|"
    r"yarn|pnpm|python|py|node|start-process|start|explorer|code|notepad|invoke-item|ii|set-clipboard|"
    r"rename|attrib|tar|7z)\b"
)


def _segments(command: str) -> list[str]:
    parts = re.split(r";|\|\||&&|\r?\n|(?<!\|)\|(?!\|)", command)
    return [p.strip().lstrip("&(@ ").strip() for p in parts if p.strip()]


def classify_command(command: str) -> CommandRisk:
    text = command.strip()
    if not text:
        return CommandRisk(Risk.READ_ONLY, "empty command")
    for risk, pattern, why in (
        (Risk.CREDENTIAL_ACCESS, _CREDENTIAL, "reads credentials or secrets"),
        (Risk.PRIVILEGED, _PRIVILEGED, "changes system configuration, needs elevation or executes hidden code"),
        (Risk.DESTRUCTIVE, _DESTRUCTIVE, "deletes data or ends processes"),
        (Risk.NETWORK_OR_EXTERNAL_TRANSFER, _NETWORK, "sends or downloads data over the network"),
    ):
        match = pattern.search(text)
        if match:
            return CommandRisk(risk, f"{why} ({match.group(0).strip()[:40]})")
    worst = Risk.READ_ONLY
    reason = "read-only"
    if _REDIRECT.search(text):
        worst, reason = Risk.MODIFICATION, "writes a file through redirection"
    for segment in _segments(text):
        if _READ_ONLY_LEAD.match(segment):
            continue
        if _LOW_RISK_LEAD.match(segment):
            if worst < Risk.LOW_RISK_REVERSIBLE:
                worst, reason = Risk.LOW_RISK_REVERSIBLE, f"low-risk change ({segment[:40]})"
            continue
        if _MODIFICATION_LEAD.match(segment):
            if worst < Risk.MODIFICATION:
                worst, reason = Risk.MODIFICATION, f"modifies files or runs a program ({segment[:40]})"
            continue
        if worst < Risk.UNKNOWN:
            worst, reason = Risk.UNKNOWN, f"unrecognised command ({segment[:40]})"
    return CommandRisk(worst, reason)


_CREDENTIAL_PATH = re.compile(
    r"(?i)(?:^|[\\/])(?:\.env(?:\.[\w.-]+)?|id_rsa|id_ed25519|id_ecdsa|[\w.-]+\.pem|[\w.-]+\.pfx|[\w.-]+\.p12|"
    r"[\w.-]+\.key|\.git-credentials|\.npmrc|\.pypirc|credentials(?:\.json)?|secrets?\.(?:json|ya?ml|toml)|"
    r"login data|cookies|[\w.-]+\.kdbx)$"
)


def is_credential_path(path: str) -> bool:
    return bool(_CREDENTIAL_PATH.search(str(path).strip().strip("'\"")))
