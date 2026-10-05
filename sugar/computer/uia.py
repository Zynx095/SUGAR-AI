"""UI Automation (UIA) through comtypes — level 2 of computer control.

Used where the Win32 API can't see inside a window: browser tabs and the
address bar, an editor's text (to verify typing), dialog buttons (save
prompts), and named elements to click.

Chromium browsers expose the whole web page through UIA once a client asks
for it, so walking "all descendants" of a browser window can take a second
or more. Everything that only needs the browser's own UI (tab strip, address
bar) walks the tree breadth-first *around* the document, and the elements it
finds are cached per window.

Only ever called from the ``sugar-desktop`` worker thread, which initialises
COM as multi-threaded (the apartment UIA clients should use).
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from typing import Any

from sugar.computer.backend import FocusInfo, TabInfo

log = logging.getLogger(__name__)

CONTROL_TYPES = {
    50000: "button", 50001: "calendar", 50002: "checkbox", 50003: "combobox", 50004: "edit", 50005: "link",
    50006: "image", 50007: "listitem", 50008: "list", 50009: "menu", 50010: "menubar", 50011: "menuitem",
    50012: "progressbar", 50013: "radiobutton", 50014: "scrollbar", 50015: "slider", 50016: "spinner",
    50017: "statusbar", 50018: "tab", 50019: "tabitem", 50020: "text", 50021: "toolbar", 50022: "tooltip",
    50023: "tree", 50024: "treeitem", 50025: "custom", 50026: "group", 50027: "thumb", 50028: "datagrid",
    50029: "dataitem", 50030: "document", 50031: "splitbutton", 50032: "window", 50033: "pane",
    50034: "header", 50035: "headeritem", 50036: "table", 50037: "titlebar", 50038: "separator",
}
_TYPE_IDS = {name: type_id for type_id, name in CONTROL_TYPES.items()}
CLICKABLE = ("button", "link", "menuitem", "listitem", "tabitem", "checkbox", "radiobutton", "treeitem",
             "splitbutton", "dataitem", "image", "text")
_DOCUMENT = 50030
_MATCH_SUBSTRING_IGNORE_CASE = 3  # PropertyConditionFlags_IgnoreCase | PropertyConditionFlags_MatchSubstring


class Accessibility:
    def __init__(self) -> None:
        self._auto: Any = None
        self._U: Any = None
        self._cache: dict[tuple[int, str], Any] = {}

    # ------------------------------------------------------------------ setup

    def _client(self) -> tuple[Any, Any]:
        if self._auto is None:
            import comtypes.client

            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient

            self._U = UIAutomationClient
            self._auto = comtypes.client.CreateObject(UIAutomationClient.CUIAutomation,
                                                      interface=UIAutomationClient.IUIAutomation)
        return self._auto, self._U

    def warm_up(self) -> None:
        self._client()

    def _element(self, hwnd: int) -> Any | None:
        auto, _ = self._client()
        try:
            return auto.ElementFromHandle(hwnd)
        except Exception:
            return None

    @staticmethod
    def _safe(fn: Callable[[], Any], default: Any = None) -> Any:
        try:
            return fn()
        except Exception:
            return default

    def _pattern(self, element: Any, pattern: str) -> Any | None:
        _, U = self._client()
        ids = {
            "invoke": (U.UIA_InvokePatternId, U.IUIAutomationInvokePattern),
            "selection_item": (U.UIA_SelectionItemPatternId, U.IUIAutomationSelectionItemPattern),
            "value": (U.UIA_ValuePatternId, U.IUIAutomationValuePattern),
            "text": (U.UIA_TextPatternId, U.IUIAutomationTextPattern),
            "toggle": (U.UIA_TogglePatternId, U.IUIAutomationTogglePattern),
            "expand": (U.UIA_ExpandCollapsePatternId, U.IUIAutomationExpandCollapsePattern),
            "legacy": (U.UIA_LegacyIAccessiblePatternId, U.IUIAutomationLegacyIAccessiblePattern),
        }
        pattern_id, interface = ids[pattern]
        try:
            raw = element.GetCurrentPattern(pattern_id)
            return raw.QueryInterface(interface) if raw else None
        except Exception:
            return None

    def _walk(self, root: Any, want: Callable[[Any, int], bool], *, max_nodes: int = 500, max_depth: int = 14,
              skip_documents: bool = True, collect: bool = False) -> list[Any]:
        """Breadth-first search of the control view, optionally not entering web documents."""
        auto, _ = self._client()
        walker = auto.ControlViewWalker
        found: list[Any] = []
        queue: deque[tuple[Any, int]] = deque([(root, 0)])
        seen = 0
        while queue:
            element, depth = queue.popleft()
            child = self._safe(lambda e=element: walker.GetFirstChildElement(e))
            while child:  # comtypes returns a NULL (falsy) pointer, not None, at the end of a level
                seen += 1
                control_type = self._safe(lambda c=child: c.CurrentControlType, 0)
                if want(child, control_type):
                    found.append(child)
                    if not collect:
                        return found
                if depth < max_depth and not (skip_documents and control_type == _DOCUMENT):
                    queue.append((child, depth + 1))
                if seen >= max_nodes:
                    return found
                child = self._safe(lambda c=child: walker.GetNextSiblingElement(c))
        return found

    def _cached(self, hwnd: int, key: str, finder: Callable[[Any], Any | None]) -> Any | None:
        element = self._cache.get((hwnd, key))
        if element is not None and self._safe(lambda: element.CurrentProcessId, None) is not None:
            return element
        root = self._element(hwnd)
        if root is None:
            return None
        element = finder(root)
        if element is not None:
            self._cache[(hwnd, key)] = element
        return element

    def forget(self, hwnd: int) -> None:
        for key in [k for k in self._cache if k[0] == hwnd]:
            self._cache.pop(key, None)

    # ------------------------------------------------------------------ tabs

    def _tab_strip(self, hwnd: int) -> Any | None:
        def find(root: Any) -> Any | None:
            hits = self._walk(root, lambda el, ct: ct == _TYPE_IDS["tab"], max_nodes=400)
            return hits[0] if hits else None

        return self._cached(hwnd, "tabstrip", find)

    def _tab_items(self, hwnd: int) -> list[Any]:
        auto, U = self._client()
        strip = self._tab_strip(hwnd)
        if strip is None:
            return []
        condition = auto.CreatePropertyCondition(U.UIA_ControlTypePropertyId, U.UIA_TabItemControlTypeId)
        items = self._safe(lambda: strip.FindAll(U.TreeScope_Children, condition))
        if items is None:
            self.forget(hwnd)
            return []
        if items.Length == 0:
            items = self._safe(lambda: strip.FindAll(U.TreeScope_Descendants, condition))
            if items is None:
                return []
        return [items.GetElement(i) for i in range(items.Length)]

    def tabs(self, hwnd: int) -> list[TabInfo]:
        result = []
        for index, item in enumerate(self._tab_items(hwnd)):
            name = self._safe(lambda it=item: it.CurrentName, "") or ""
            selection = self._pattern(item, "selection_item")
            selected = bool(self._safe(lambda s=selection: s.CurrentIsSelected, False)) if selection else False
            result.append(TabInfo(index, name, selected))
        return result

    def select_tab(self, hwnd: int, index: int) -> bool:
        items = self._tab_items(hwnd)
        if not 0 <= index < len(items):
            return False
        item = items[index]
        selection = self._pattern(item, "selection_item")
        if selection is not None and self._safe(lambda: selection.Select() or True, False):
            return True
        legacy = self._pattern(item, "legacy")
        return bool(legacy is not None and self._safe(lambda: legacy.DoDefaultAction() or True, False))

    def close_tab(self, hwnd: int, index: int) -> bool:
        auto, U = self._client()
        items = self._tab_items(hwnd)
        if not 0 <= index < len(items):
            return False
        condition = auto.CreatePropertyCondition(U.UIA_ControlTypePropertyId, U.UIA_ButtonControlTypeId)
        button = self._safe(lambda: items[index].FindFirst(U.TreeScope_Descendants, condition))
        if not button:  # None or a NULL COM pointer
            return False
        invoke = self._pattern(button, "invoke")
        return bool(invoke is not None and self._safe(lambda: invoke.Invoke() or True, False))

    # ------------------------------------------------------------------ browser address bar

    def address(self, hwnd: int) -> str | None:
        def find(root: Any) -> Any | None:
            def is_address(el: Any, ct: int) -> bool:
                if ct != _TYPE_IDS["edit"]:
                    return False
                name = (self._safe(lambda: el.CurrentName, "") or "").lower()
                return "address" in name or "search bar" in name or "enter address" in name

            hits = self._walk(root, is_address, max_nodes=500)
            return hits[0] if hits else None

        edit = self._cached(hwnd, "address", find)
        if edit is None:
            return None
        value = self._pattern(edit, "value")
        text = self._safe(lambda: value.CurrentValue, None) if value is not None else None
        if text is None:
            self.forget(hwnd)
        return text

    # ------------------------------------------------------------------ focus and text

    def focused(self) -> FocusInfo | None:
        auto, _ = self._client()
        element = self._safe(lambda: auto.GetFocusedElement())
        if element is None:
            return None
        control_type = CONTROL_TYPES.get(self._safe(lambda: element.CurrentControlType, 0), "unknown")
        return FocusInfo(control_type, self._safe(lambda: element.CurrentName, "") or "",
                         self._safe(lambda: element.CurrentClassName, "") or "")

    def focused_text(self, limit: int = 20000) -> str | None:
        """Text of the focused control: the end of the document around the caret when possible."""
        auto, U = self._client()
        element = self._safe(lambda: auto.GetFocusedElement())
        if element is None:
            return None
        text_pattern = self._pattern(element, "text")
        if text_pattern is not None:
            text = self._safe(lambda: text_pattern.DocumentRange.GetText(-1))
            if text is not None:
                return text[-limit:]
        value = self._pattern(element, "value")
        if value is not None:
            text = self._safe(lambda: value.CurrentValue)
            if text is not None:
                return text[-limit:]
        return None

    def page_text(self, hwnd: int, limit: int = 20000) -> str | None:
        root = self._element(hwnd)
        if root is None:
            return None
        documents = self._walk(root, lambda el, ct: ct == _DOCUMENT, max_nodes=400)
        if not documents:
            return None
        pattern = self._pattern(documents[0], "text")
        if pattern is None:
            return None
        return self._safe(lambda: pattern.DocumentRange.GetText(limit))

    # ------------------------------------------------------------------ named elements

    def _find_named(self, hwnd: int, name: str, kinds: tuple[str, ...]) -> list[Any]:
        auto, U = self._client()
        root = self._element(hwnd)
        if root is None:
            return []
        type_ids = [_TYPE_IDS[k] for k in (kinds or CLICKABLE) if k in _TYPE_IDS]
        type_conditions = [auto.CreatePropertyCondition(U.UIA_ControlTypePropertyId, t) for t in type_ids]
        type_condition = type_conditions[0]
        for extra in type_conditions[1:]:
            type_condition = auto.CreateOrCondition(type_condition, extra)
        name_condition = auto.CreatePropertyConditionEx(U.UIA_NamePropertyId, name, _MATCH_SUBSTRING_IGNORE_CASE)
        found = self._safe(lambda: root.FindAll(U.TreeScope_Descendants,
                                                auto.CreateAndCondition(name_condition, type_condition)))
        if found is None:
            return []
        return [found.GetElement(i) for i in range(min(found.Length, 60))]

    def invoke(self, hwnd: int, name: str, kinds: tuple[str, ...] = ()) -> str | None:
        """Activate the element whose name best matches; returns its name, or None."""
        candidates = self._find_named(hwnd, name, kinds)
        wanted = name.strip().lower()

        def rank(element: Any) -> tuple[int, int]:
            label = (self._safe(lambda: element.CurrentName, "") or "").strip().lower()
            offscreen = bool(self._safe(lambda: element.CurrentIsOffscreen, False))
            closeness = 0 if label == wanted else 1 if label.startswith(wanted) else 2
            return (offscreen, closeness * 1000 + len(label))

        for element in sorted(candidates, key=rank):
            label = self._safe(lambda e=element: e.CurrentName, "") or name
            for pattern, action in (("invoke", "Invoke"), ("selection_item", "Select"), ("toggle", "Toggle"),
                                    ("expand", "Expand"), ("legacy", "DoDefaultAction")):
                handle = self._pattern(element, pattern)
                if handle is not None and self._safe(lambda h=handle, a=action: getattr(h, a)() or True, False):
                    return label
        return None

    def element_center(self, hwnd: int, name: str, kinds: tuple[str, ...] = ()) -> tuple[int, int] | None:
        for element in self._find_named(hwnd, name, kinds):
            rect = self._safe(lambda e=element: e.CurrentBoundingRectangle)
            if rect is not None and rect.right > rect.left and rect.bottom > rect.top:
                return ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
        return None

    def buttons(self, hwnd: int) -> list[str]:
        root = self._element(hwnd)
        if root is None:
            return []
        hits = self._walk(root, lambda el, ct: ct == _TYPE_IDS["button"], max_nodes=700, collect=True)
        names = []
        for element in hits:
            label = (self._safe(lambda e=element: e.CurrentName, "") or "").strip()
            if label and label not in names:
                names.append(label)
        return names

    def describe_ui(self, hwnd: int, limit: int = 150) -> list[dict[str, Any]]:
        root = self._element(hwnd)
        if root is None:
            return []
        interesting = {_TYPE_IDS[k] for k in (*CLICKABLE, "edit", "combobox", "document", "tab", "window")}
        hits = self._walk(root, lambda el, ct: ct in interesting, max_nodes=limit * 6, collect=True,
                          skip_documents=False)
        described = []
        for element in hits[:limit]:
            name = (self._safe(lambda e=element: e.CurrentName, "") or "").strip()
            control_type = CONTROL_TYPES.get(self._safe(lambda e=element: e.CurrentControlType, 0), "unknown")
            if not name and control_type not in ("edit", "document"):
                continue
            rect = self._safe(lambda e=element: e.CurrentBoundingRectangle)
            described.append({
                "type": control_type,
                "name": name[:120],
                "rect": [rect.left, rect.top, rect.right, rect.bottom] if rect is not None else None,
                "offscreen": bool(self._safe(lambda e=element: e.CurrentIsOffscreen, False)),
            })
        return described
