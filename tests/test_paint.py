"""Drawing in Paint: plans (vector and traced), reference choice and licences, drawing with real mouse drags in
the canvas only, stopping when the user takes the mouse, follow-ups, and saving without silent overwrites."""

from __future__ import annotations

import math
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from fake_desktop import FakeDesktop, FakeWindow
from PIL import Image, ImageDraw

from sugar.computer.drawing import Plan, densify, drawability, reference_plan, vector_plan
from sugar.computer.paint import PaintController
from sugar.computer.references import download, search
from sugar.computer.results import ComputerActionResult
from sugar.tools.paint import resolve_path

# ---------------------------------------------------------------------------------------------- plans


@pytest.mark.parametrize("subject", ["circle", "square", "triangle", "star", "heart", "house", "sun", "smiley face",
                                     "tree", "flower", "a circle"])
def test_simple_subjects_have_vector_plans(subject):
    plan = vector_plan(subject)
    assert plan is not None and plan.strokes and all(len(s) >= 2 for s in plan.strokes)
    x0, y0, x1, y1 = plan.bbox()
    assert 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1


def test_placing_keeps_proportions_inside_the_box():
    plan = vector_plan("circle").placed((0.6, 0.2, 0.9, 0.8))
    x0, y0, x1, y1 = plan.bbox()
    assert 0.6 - 1e-9 <= x0 and x1 <= 0.9 + 1e-9 and math.isclose(x1 - x0, y1 - y0, rel_tol=0.05)


def silhouette() -> Image.Image:
    image = Image.new("RGB", (400, 300), "white")
    d = ImageDraw.Draw(image)
    d.ellipse((120, 90, 280, 250), fill="black")  # body
    d.polygon([(130, 110), (150, 40), (180, 100)], fill="black")  # ears
    d.polygon([(220, 100), (250, 40), (270, 110)], fill="black")
    return image


def test_reference_is_traced_into_strokes_with_a_fill_point():
    plan = reference_plan(silhouette(), "cat")
    assert plan.source == "reference" and plan.strokes and plan.points < 1400
    assert plan.fill_point is not None
    x0, y0, x1, y1 = plan.bbox()
    assert 0 <= x0 and x1 <= 1 and 0 <= y0 and y1 <= 1


def test_drawability_prefers_a_clean_subject_over_a_busy_photo():
    noise = Image.fromarray(np.random.default_rng(1).integers(0, 255, (300, 400, 3), dtype=np.uint8))
    assert drawability(silhouette()) > 0.5 > drawability(noise)


def test_densify_makes_small_mouse_steps():
    points = densify([(0, 0), (100, 0)], 5)
    assert len(points) == 21 and all(abs(b[0] - a[0]) <= 5 for a, b in zip(points, points[1:], strict=False))


# ---------------------------------------------------------------------------------------------- references


def commons_transport():
    def png(colour="black") -> bytes:
        import io

        buffer = io.BytesIO()
        silhouette().save(buffer, format="PNG")
        return buffer.getvalue()

    def handler(request: httpx.Request) -> httpx.Response:
        if "api.php" in str(request.url):
            pages = {
                "1": {"title": "File:Cat line art.png", "index": 1, "imageinfo": [{
                    "thumburl": "https://upload.example/cat.png", "descriptionurl": "https://commons.example/cat",
                    "extmetadata": {"LicenseShortName": {"value": "CC BY-SA 4.0"}, "Artist": {"value": "<a>Ann</a>"}}}]},
                "2": {"title": "File:Copyrighted cat.jpg", "index": 2, "imageinfo": [{
                    "thumburl": "https://upload.example/c.jpg", "extmetadata": {
                        "LicenseShortName": {"value": "All rights reserved"}}}]},
                "3": {"title": "File:Cat program.exe.png", "index": 3, "imageinfo": [{
                    "thumburl": "https://upload.example/evil", "extmetadata": {"LicenseShortName": {"value": "CC0"}}}]},
            }
            return httpx.Response(200, json={"query": {"pages": pages}})
        if request.url.path.endswith("cat.png"):
            return httpx.Response(200, content=png(), headers={"content-type": "image/png"})
        return httpx.Response(200, content=b"MZ\x90\x00", headers={"content-type": "application/octet-stream"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_only_freely_licensed_images_and_only_png_or_jpeg_are_used():
    client = commons_transport()
    found = search("cat", client=client)
    titles = [r.title for r in found]
    assert "Cat line art.png" in titles and "Copyrighted cat.jpg" not in titles
    good = next(r for r in found if r.title == "Cat line art.png")
    assert good.author == "Ann" and good.licence == "CC BY-SA 4.0"
    assert download(good, client=client) is not None
    executable = next(r for r in found if r.title == "Cat program.exe.png")
    assert download(executable, client=client) is None  # not an image type: never downloaded as one


# ---------------------------------------------------------------------------------------------- drawing


class Canvas:
    """window_image(): a Paint-like window with a white canvas that 'gets ink' once drawing has happened."""

    def __init__(self, desktop):
        self.desktop = desktop

    def __call__(self, window):
        image = Image.new("RGB", (800, 600), (240, 240, 240))
        d = ImageDraw.Draw(image)
        d.rectangle((100, 150, 700, 550), fill="white")  # the canvas, in window coordinates
        if any(m[0] == "down" for m in self.desktop.mouse):
            d.line((200, 200, 600, 500), fill="black", width=3)
        return image


def paint_setup():
    desktop = FakeDesktop()
    window = desktop.add(FakeWindow("Untitled - Paint", "mspaint.exe", 900, kind="other", rect=(0, 0, 800, 600)))
    clicks = []
    computer = SimpleNamespace(
        windows=SimpleNamespace(list=lambda: [w.info() for w in desktop.windows.values()]),
        apps=SimpleNamespace(open=lambda name: ComputerActionResult.ok("app.open", name, "Opened")),
        interact=SimpleNamespace(click=lambda name, hwnd=None, action="click": clicks.append(name) or
                                 ComputerActionResult.ok("ui.click", "paint", f"Clicked {name}")),
        keyboard=SimpleNamespace(chord=lambda mods, vk: desktop.key_log.append(("chord", tuple(mods), vk)),
                                 type_fast=lambda text, hwnd, gap=0: True, tap=lambda vk: None),
        window_image=Canvas(desktop))
    controller = PaintController(desktop, computer, sleep=lambda s: None)
    controller.move_delay_s = 0
    return desktop, window, controller, clicks


def test_paint_canvas_is_found_in_the_screenshot():
    desktop, window, controller, _ = paint_setup()
    assert controller.canvas(window.info()) == (100, 150, 701, 551)


def test_drawing_uses_real_drags_inside_the_canvas_and_is_verified():
    desktop, window, controller, _ = paint_setup()
    result = controller.draw_plan(vector_plan("square"), (0.2, 0.2, 0.8, 0.8))
    assert result.success and result.verified and result.data["strokes"] == 1
    downs = [m for m in desktop.mouse if m[0] == "down"]
    ups = [m for m in desktop.mouse if m[0] == "up"]
    assert len(downs) == len(ups) == 1
    moves = [m for m in desktop.mouse if m[0] == "move"][1:-1]
    assert moves and all(100 <= x <= 701 and 150 <= y <= 551 for _, x, y in moves)
    assert controller.session.shapes[-1].plan.subject == "square"


def test_drawing_stops_when_the_user_moves_the_mouse():
    desktop, window, controller, _ = paint_setup()
    original = desktop.mouse_move
    count = [0]

    def nudged(x, y):
        original(x, y)
        count[0] += 1
        if count[0] == 6:
            desktop.pointer = (5, 5)  # the user grabbed the mouse

    desktop.mouse_move = nudged
    result = controller.draw_plan(vector_plan("circle"), (0.2, 0.2, 0.8, 0.8))
    assert not result.success and "moved the mouse" in result.details
    assert [m[0] for m in desktop.mouse].count("down") == [m[0] for m in desktop.mouse].count("up")  # button released


def test_drawing_stops_if_paint_is_no_longer_in_front():
    desktop, window, controller, _ = paint_setup()
    controller.ensure_open = lambda: (window.info(), None)  # Paint was open; then the user switched away
    desktop.add(FakeWindow("Other", "notepad.exe", 901))
    result = controller.draw_plan(vector_plan("house"), (0.2, 0.2, 0.8, 0.8))
    assert not result.success and "isn't in front" in result.details
    assert not any(m[0] == "down" for m in desktop.mouse)


def test_follow_ups_place_undo_and_fill():
    desktop, window, controller, clicks = paint_setup()
    controller.draw_plan(vector_plan("circle"), controller.next_box(None))
    beside = controller.next_box("right")
    assert beside[0] >= controller.session.shapes[-1].box[2] - 1e-6 and beside[2] - beside[0] >= 0.17
    controller.draw_plan(vector_plan("tree"), beside)
    strokes = controller.session.shapes[-1].strokes
    undone = controller.undo_last()
    assert undone.success and "tree" in undone.details
    assert [k for k in desktop.key_log if k[0] == "chord"][-strokes:] == [("chord", (0x11,), ord("Z"))] * strokes
    filled = controller.fill("red")
    assert filled.success and "Fill" in clicks and any("Red" in c for c in clicks)


def test_saving_never_overwrites_without_confirmation(tmp_path):
    desktop, window, controller, _ = paint_setup()
    existing = tmp_path / "cat.png"
    existing.write_bytes(b"old")
    result = controller.save(existing)
    assert not result.success and result.data["waiting_for"] == "overwrite"
    assert existing.read_bytes() == b"old"


def test_save_names_go_to_pictures_with_png(monkeypatch, tmp_path):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert resolve_path("cat.png") == tmp_path / "Pictures" / "SID" / "cat.png"
    assert resolve_path("my cat") == tmp_path / "Pictures" / "SID" / "my cat.png"


def test_overwriting_an_existing_file_needs_the_destructive_level(monkeypatch, tmp_path):
    from sugar.tools.paint import register
    from sugar.tools.registry import PermissionLevel, ToolRegistry

    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    registry = ToolRegistry()
    register(registry, SimpleNamespace(computer=SimpleNamespace(), bus=None, data_dir=tmp_path))
    tool = registry.get("paint.save")
    assert tool.level_for({"name": "new.png"}) == PermissionLevel.NON_DESTRUCTIVE
    (tmp_path / "Pictures" / "SID").mkdir(parents=True)
    (tmp_path / "Pictures" / "SID" / "cat.png").write_bytes(b"x")
    assert tool.level_for({"name": "cat.png"}) == PermissionLevel.DESTRUCTIVE


def test_plan_object_basics():
    plan = Plan("x", [[(0.0, 0.0), (1.0, 1.0)]])
    assert plan.points == 2 and plan.bbox() == (0.0, 0.0, 1.0, 1.0)
