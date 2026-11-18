"""Paint tools: draw a subject (vector plan or a traced, freely licensed reference), colour it, thicken it, undo
it, draw something beside it, clean it up, save it."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from sugar.computer.drawing import reference_plan, vector_plan
from sugar.computer.references import find_reference
from sugar.tools.registry import PermissionLevel, Tool, ToolRegistry, ToolResult, params
from sugar.tools.services import ToolServices

L = PermissionLevel
GROUP = frozenset({"agent", "computer"})


def default_folder() -> Path:
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Pictures" / "SID"


def resolve_path(name: str) -> Path:
    name = name.strip().strip('"').strip("'")
    name = re.sub(r"\s+dot\s+(png|jpg|jpeg|bmp)$", r".\1", name, flags=re.I)
    path = Path(name)
    if not path.suffix:
        path = path.with_suffix(".png")
    if not path.is_absolute():
        path = default_folder() / path
    return path


def register(registry: ToolRegistry, services: ToolServices) -> None:
    computer = services.computer
    bus = services.bus
    folder = services.data_dir / "references"

    def paint():
        if getattr(computer, "paint", None) is None:
            from sugar.computer.paint import PaintController

            computer.paint = PaintController(computer.backend, computer)
        return computer.paint

    def activity(active: bool, **data: Any) -> None:
        bus.publish("activity", kind="drawing", active=active, **data)

    async def plan_for(subject: str, detail: float = 1.0):
        plan = vector_plan(subject)
        if plan is not None:
            return plan, None
        controller = paint()
        reference = controller.session.references.get(subject)
        if reference is None:
            if computer.settings.paint_reference_source == "none":
                return None, "I only draw simple shapes without references, and online references are turned off."
            bus.publish("activity", kind="browsing", active=True, purpose="drawing reference")
            try:
                reference = await asyncio.to_thread(find_reference, subject, vision=computer.vision, folder=folder)
            finally:
                bus.publish("activity", kind="browsing", active=False)
            if reference is None:
                return None, (f"I couldn't find a freely licensed reference for {subject} that I could trace. "
                              "Try a simpler subject, or give me an image.")
            controller.session.references[subject] = reference
        plan = await asyncio.to_thread(reference_plan, reference.image, subject, detail=detail)
        plan.attribution = reference.attribution()
        return plan, None

    async def draw(args: dict[str, Any]) -> ToolResult:
        subject = re.sub(r"^(?:a|an|the|some)\s+", "", str(args["subject"]).strip(), flags=re.I) or "circle"
        controller = paint()
        plan, problem = await plan_for(subject, float(args.get("detail") or 1.0))
        if plan is None:
            return ToolResult.failure(problem or f"I can't draw {subject}.")
        if computer.settings.paint_show_reference and plan.attribution.get("page"):
            await computer.run(computer.browser.open, None, plan.attribution["page"])
        activity(True, subject=subject)
        try:
            def run_drawing():
                window, error = controller.ensure_open()
                if error is not None:
                    return error
                if not controller.session.tool_ready:
                    controller.select_drawing_tool(window)
                    controller.select_colour(window, "black")
                    controller.session.tool_ready = True
                return controller.draw_plan(plan, controller.next_box(args.get("place")),
                                            cancel=computer.cancel_event)

            computer.cancel_event.clear()
            result = await computer.run(run_drawing)
        finally:
            activity(False)
        tool_result = result.to_tool_result()
        attribution = plan.attribution
        if result.success and attribution:
            tool_result.summary = (f"Drew a {subject} in Paint, traced from a reference on Wikimedia Commons "
                                   f"({attribution['licence']}).")
            tool_result.display = (f"Reference: [{attribution['title']}]({attribution['page']}) by "
                                   f"{attribution['author']} — {attribution['licence']}")
            tool_result.data["reference"] = attribution
        elif result.success:
            tool_result.summary = f"Drew a {subject} in Paint."
        return tool_result

    async def fill(args: dict[str, Any]) -> ToolResult:
        result = await computer.run(paint().fill, args.get("colour"))
        return result.to_tool_result()

    async def thicker(args: dict[str, Any]) -> ToolResult:
        result = await computer.run(paint().thicker)
        return result.to_tool_result()

    async def undo(args: dict[str, Any]) -> ToolResult:
        result = await computer.run(paint().undo_last)
        return result.to_tool_result()

    async def clean(args: dict[str, Any]) -> ToolResult:
        controller = paint()
        if not controller.session.shapes:
            return ToolResult.failure("There's nothing of mine to clean up.")
        last = controller.session.shapes[-1]
        undone = await computer.run(controller.undo_last)
        if not undone.success:
            return undone.to_tool_result()
        plan, problem = await plan_for(last.plan.subject, detail=0.55)
        if plan is None:
            return ToolResult.failure(problem or "I couldn't redraw it.")
        result = await computer.run(controller.draw_plan, plan, last.box, cancel=computer.cancel_event)
        if result.success:
            return ToolResult(True, f"Redrew the {last.plan.subject} with cleaner, simpler lines.",
                              data=result.data)
        return result.to_tool_result()

    def save_level(args: dict[str, Any]) -> PermissionLevel:
        return L.DESTRUCTIVE if resolve_path(str(args.get("name") or "drawing.png")).exists() else L.NON_DESTRUCTIVE

    async def save(args: dict[str, Any]) -> ToolResult:
        path = resolve_path(str(args.get("name") or "drawing.png"))
        exists = path.exists()  # reaching here with an existing file means the user confirmed replacing it
        result = await computer.run(paint().save, path, overwrite=exists)
        return result.to_tool_result(display=f"Saved to `{path}`" if result.success else None)

    async def new(args: dict[str, Any]) -> ToolResult:
        controller = paint()

        def clear():
            window, error = controller.ensure_open()
            if error is not None:
                return error
            computer.keyboard.chord([0x11], ord("A"))
            computer.keyboard.tap(0x2E)  # Delete: the selection becomes white
            computer.keyboard.tap(0x1B)
            controller.session.shapes.clear()
            from sugar.computer.results import ComputerActionResult

            return ComputerActionResult.ok("paint.new", "paint", "Cleared the canvas.")

        result = await computer.run(clear)
        return result.to_tool_result()

    registry.register(Tool("paint.draw", "Draw a subject in Microsoft Paint with Paint's own tools: simple shapes "
                           "directly, anything else traced from a freely licensed reference image. place: right, left, "
                           "above, below (relative to the last drawing).",
                           params(["subject"], subject={"type": "string"}, place={"type": "string"},
                                  detail={"type": "number"}), draw, L.NON_DESTRUCTIVE, 240, GROUP,
                           describe=lambda a: f"draw {a.get('subject')} in Paint"))
    registry.register(Tool("paint.fill", "Colour in the last drawing (Paint's fill tool).",
                           params(colour={"type": "string"}), fill, L.NON_DESTRUCTIVE, 60, GROUP))
    registry.register(Tool("paint.thicker", "Make the last drawing's outline thicker.", params(), thicker,
                           L.NON_DESTRUCTIVE, 120, GROUP))
    registry.register(Tool("paint.undo", "Undo the last shape SID drew in Paint.", params(), undo, L.NON_DESTRUCTIVE,
                           30, GROUP))
    registry.register(Tool("paint.clean", "Redraw the last drawing with cleaner, simpler lines.", params(), clean,
                           L.NON_DESTRUCTIVE, 240, GROUP))
    registry.register(Tool("paint.save", "Save the Paint picture (a name like cat.png goes to Pictures\\SID). "
                           "Replacing an existing file asks first.", params(name={"type": "string"}), save, save_level,
                           90, GROUP, describe=lambda a: f"replace the existing {resolve_path(str(a.get('name') or 'drawing.png'))}"))
    registry.register(Tool("paint.new", "Clear Paint's canvas for a new drawing.", params(), new, L.NON_DESTRUCTIVE,
                           30, GROUP))
