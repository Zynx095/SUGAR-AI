"""Visible UI elements and how a spoken description picks one.

"Click Settings", "select the second option", "click the button labelled Save", "click the checkbox",
"open the first result", "choose the option on the left", "click the search box".

:func:`parse_target` turns the words into a :class:`TargetSpec` (name words, control kinds, an ordinal, a
screen region); :func:`choose` ranks a fresh snapshot of the window's elements against it and either picks
exactly one, or reports that several fit equally well (so SID asks instead of guessing), or that none do.
Pure functions over plain data: the UI Automation snapshot is taken by the backend, and the same logic is
used for elements found by OCR or a vision model.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

Rect = tuple[int, int, int, int]  # left, top, right, bottom (screen pixels)


@dataclass(frozen=True)
class Element:
    id: int
    name: str
    kind: str  # UIA control type name: button, link, checkbox, edit, listitem, …
    rect: Rect
    enabled: bool = True
    offscreen: bool = False
    patterns: frozenset[str] = frozenset()  # invoke, toggle, select, expand, value, scroll, text
    toggled: bool | None = None
    selected: bool | None = None
    expanded: bool | None = None
    value: str | None = None
    automation_id: str = ""
    help: str = ""  # help text / placeholder
    source: str = "uia"  # uia | ocr | vision

    @property
    def center(self) -> tuple[int, int]:
        return ((self.rect[0] + self.rect[2]) // 2, (self.rect[1] + self.rect[3]) // 2)

    @property
    def area(self) -> int:
        return max(0, self.rect[2] - self.rect[0]) * max(0, self.rect[3] - self.rect[1])

    @property
    def label(self) -> str:
        name = self.name.strip() or self.help.strip() or self.automation_id
        return f"{name} {KIND_LABEL.get(self.kind, self.kind)}".strip() if name else KIND_LABEL.get(self.kind, self.kind)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name[:120], "kind": self.kind, "rect": list(self.rect),
                "toggled": self.toggled, "selected": self.selected, "value": (self.value or "")[:80] or None}


KIND_LABEL = {"button": "button", "splitbutton": "button", "link": "link", "checkbox": "checkbox",
              "radiobutton": "option", "listitem": "item", "menuitem": "menu item", "tabitem": "tab",
              "edit": "box", "combobox": "dropdown", "treeitem": "item", "dataitem": "item", "slider": "slider",
              "hyperlink": "link", "text": "text", "image": "image", "document": "document"}

CLICKABLE = {"button", "splitbutton", "link", "checkbox", "radiobutton", "listitem", "menuitem", "tabitem",
             "treeitem", "dataitem", "edit", "combobox", "image", "slider", "hyperlink"}

# spoken kind words → UIA kinds (and a soft name hint)
KIND_WORDS: dict[str, tuple[tuple[str, ...], str | None]] = {
    "search box": (("edit", "combobox"), "search"), "search bar": (("edit", "combobox"), "search"),
    "search field": (("edit", "combobox"), "search"), "address bar": (("edit", "combobox"), "address"),
    "text box": (("edit",), None), "text field": (("edit",), None), "input box": (("edit",), None),
    "field": (("edit", "combobox"), None), "box": (("edit", "combobox", "checkbox"), None),
    "check box": (("checkbox",), None), "checkbox": (("checkbox",), None), "tick box": (("checkbox",), None),
    "radio button": (("radiobutton",), None), "button": (("button", "splitbutton"), None),
    "link": (("link", "hyperlink"), None), "tab": (("tabitem",), None),
    "menu item": (("menuitem",), None), "menu": (("menuitem", "button", "splitbutton"), None),
    "option": (("radiobutton", "listitem", "menuitem", "checkbox", "dataitem", "treeitem", "tabitem"), None),
    "choice": (("radiobutton", "listitem", "menuitem", "checkbox"), None),
    "item": (("listitem", "treeitem", "dataitem", "menuitem"), None),
    "result": (("link", "hyperlink", "listitem", "dataitem"), None),
    "dropdown": (("combobox",), None), "drop down": (("combobox",), None), "combo box": (("combobox",), None),
    "list": (("combobox", "list"), None), "toggle": (("button", "checkbox"), None),
    "switch": (("button", "checkbox"), None), "slider": (("slider",), None),
    "icon": (("button", "image", "listitem"), None), "image": (("image",), None),
    "picture": (("image",), None), "heading": (("text",), None),
}
ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
            "fifth": 5, "5th": 5, "sixth": 6, "6th": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
            "last": -1, "top": 1, "next": None}
REGIONS = {"left": "left", "right": "right", "top": "top", "bottom": "bottom", "middle": "center",
           "center": "center", "centre": "center", "upper": "top", "lower": "bottom"}

_KIND_RE = "|".join(sorted((re.escape(k) for k in KIND_WORDS), key=len, reverse=True))
_ORD_RE = "|".join(k for k in ORDINALS if ORDINALS[k] is not None)
_REGION_RE = r"(?:on|at|in|from) the (?P<region>left|right|top|bottom|middle|center|centre|upper|lower)(?: side| corner| half| part)?"


@dataclass
class TargetSpec:
    text: str = ""  # name words ("save", "settings"); empty when only the kind or position identifies it
    kinds: tuple[str, ...] = ()
    hint: str | None = None  # soft name hint for kinds like "search box"
    ordinal: int | None = None
    region: str | None = None
    raw: str = ""

    def describe(self) -> str:
        return self.raw or self.text


def parse_target(phrase: str) -> TargetSpec:
    raw = phrase.strip().strip(".!?").strip()
    t = raw.lower()
    t = re.sub(r"^(?:on |at )", "", t)
    spec = TargetSpec(raw=raw)
    match = re.search(_REGION_RE, t)
    if match:
        spec.region = REGIONS[match.group("region")]
        t = (t[: match.start()] + t[match.end():]).strip()
    match = re.search(rf"\b(?:the |a |an )?(?P<ord>{_ORD_RE})\b", t)
    if match:
        spec.ordinal = ORDINALS[match.group("ord")]
        t = (t[: match.start()] + " " + t[match.end():]).strip()
    named = re.search(r"\b(?:labell?ed|called|named|that says|saying|with the (?:text|label|name)|titled)\s+(?P<name>.+)$", t)
    if named:
        spec.text = named.group("name").strip(" '\"")
        t = t[: named.start()].strip()
    match = re.search(rf"\b(?P<kind>{_KIND_RE})s?\b", t)
    if match:
        kinds, hint = KIND_WORDS[match.group("kind")]
        spec.kinds, spec.hint = kinds, hint
        rest = (t[: match.start()] + " " + t[match.end():]).strip()
    else:
        rest = t
    rest = re.sub(r"^(?:the|a|an|this|that|my)(?:\s+|$)", "", rest).strip()
    rest = re.sub(r"\s+(?:one|thing)$", "", rest).strip()
    if not spec.text and rest and rest not in ("one", "thing", "it", "this", "that"):
        spec.text = rest.strip(" '\"")
    return spec


# ----------------------------------------------------------------------------------------- ranking


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text.lower())).strip()


def name_score(element: Element, text: str) -> float:
    """How well the element's visible name (or help text / id) matches the spoken name, 0–1."""
    wanted = _norm(text)
    if not wanted:
        return 0.5
    best = 0.0
    for candidate in (element.name, element.help, element.automation_id.replace("_", " ")):
        label = _norm(candidate)
        if not label:
            continue
        if label == wanted:
            best = max(best, 1.0)
        elif label.startswith(wanted + " ") or label.startswith(wanted):
            best = max(best, 0.86)
        elif re.search(rf"\b{re.escape(wanted)}\b", label):
            best = max(best, 0.78)
        else:
            ratio = difflib.SequenceMatcher(None, wanted, label).ratio()
            if ratio >= 0.8:
                best = max(best, 0.6 * ratio)
    return best


def _in_region(element: Element, region: str, window: Rect) -> bool:
    left, top, right, bottom = window
    width, height = max(1, right - left), max(1, bottom - top)
    x, y = element.center
    fx, fy = (x - left) / width, (y - top) / height
    return {"left": fx < 0.45, "right": fx > 0.55, "top": fy < 0.45, "bottom": fy > 0.55,
            "center": 0.25 < fx < 0.75 and 0.25 < fy < 0.75}[region]


def reading_order(elements: list[Element]) -> list[Element]:
    """Top-to-bottom, then left-to-right, treating elements whose tops are within 12 px as one row."""
    rows: list[list[Element]] = []
    for element in sorted(elements, key=lambda e: (e.rect[1], e.rect[0])):
        if rows and abs(rows[-1][0].rect[1] - element.rect[1]) <= 12:
            rows[-1].append(element)
        else:
            rows.append([element])
    return [e for row in rows for e in sorted(row, key=lambda e: e.rect[0])]


def visible(element: Element, window: Rect | None) -> bool:
    if element.offscreen or element.area <= 0 or not element.enabled:
        return False
    if window is None:
        return True
    left, top, right, bottom = window
    x, y = element.center
    return left <= x <= right and top <= y <= bottom


def _dedupe(elements: list[Element]) -> list[Element]:
    """Drop elements nested inside another with the same name (a list item and its text, a link and its span)."""
    kept: list[Element] = []
    for element in sorted(elements, key=lambda e: -e.area):
        nested = next((k for k in kept if _norm(k.name) == _norm(element.name) and k.rect[0] <= element.rect[0]
                       and k.rect[1] <= element.rect[1] and k.rect[2] >= element.rect[2]
                       and k.rect[3] >= element.rect[3]), None)
        if nested is None:
            kept.append(element)
        elif element.kind in CLICKABLE and nested.kind not in CLICKABLE:
            kept[kept.index(nested)] = element  # keep the actionable one
    return kept


@dataclass
class Choice:
    element: Element | None
    candidates: list[Element] = field(default_factory=list)
    ambiguous: bool = False
    reason: str = ""


def choose(elements: list[Element], spec: TargetSpec, window: Rect | None = None) -> Choice:
    """Pick the one element the spec means, or say why not (none / several equally likely)."""
    pool = [e for e in elements if visible(e, window)]
    if spec.kinds:
        pool = [e for e in pool if e.kind in spec.kinds]
    else:
        pool = [e for e in pool if e.kind in CLICKABLE or (spec.text and e.kind in ("text", "document", "pane"))]
    if spec.region and window is not None:
        pool = [e for e in pool if _in_region(e, spec.region, window)]
    scored: list[tuple[float, Element]] = []
    for element in pool:
        score = name_score(element, spec.text) if spec.text else 0.5
        if spec.text and score < 0.45:
            continue
        if spec.hint and spec.hint in _norm(f"{element.name} {element.help} {element.automation_id}"):
            score += 0.3
        if element.kind in CLICKABLE:
            score += 0.05
        scored.append((score, element))
    if not scored:
        return Choice(None, reason="no match")
    best = max(score for score, _ in scored)
    top = _dedupe([e for score, e in scored if score >= best - 0.04])
    if spec.ordinal is not None:
        ordered = reading_order([e for _, e in scored]) if not spec.text else reading_order(top)
        index = spec.ordinal - 1 if spec.ordinal > 0 else len(ordered) + spec.ordinal
        if 0 <= index < len(ordered):
            return Choice(ordered[index], ordered)
        return Choice(None, ordered, reason=f"only {len(ordered)} found")
    if spec.region and len(top) > 1 and window is not None:
        axis = {"left": lambda e: e.center[0], "right": lambda e: -e.center[0], "top": lambda e: e.center[1],
                "bottom": lambda e: -e.center[1], "center": lambda e: 0}[spec.region]
        top = sorted(top, key=axis)
        if spec.region != "center":
            return Choice(top[0], top)
    if len(top) == 1:
        return Choice(top[0], [e for _, e in sorted(scored, key=lambda p: -p[0])])
    return Choice(None, reading_order(top), ambiguous=True, reason="several match")


def describe_position(element: Element, window: Rect) -> str:
    """'near the top left', 'in the middle', 'at the bottom right' — for "where is the Save button?"."""
    left, top, right, bottom = window
    x, y = element.center
    fx = (x - left) / max(1, right - left)
    fy = (y - top) / max(1, bottom - top)
    vertical = "top" if fy < 0.33 else "bottom" if fy > 0.66 else "middle"
    horizontal = "left" if fx < 0.33 else "right" if fx > 0.66 else "centre"
    if vertical == "middle" and horizontal == "centre":
        return "in the middle"
    if vertical == "middle":
        return f"on the {horizontal}"
    if horizontal == "centre":
        return f"at the {vertical}"
    return f"at the {vertical} {horizontal}"
