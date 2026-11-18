"""The camera — only when the user asks, visibly, and released straight after.

Lifecycle: discover devices → open the chosen one → grab a *fresh* frame (the first frames after opening are
dropped: exposure settles) → hand it to the local vision model → release the device. Nothing runs in the
background and nothing is recorded: "turn on the camera" opens it and keeps it open (the UI shows a red
CAMERA ON badge) until "turn off the camera", 2 minutes of no use, or SID shutting down; one-off questions
("what do you see?") open, capture and release in one go.

Frames live in memory only. "Take a picture" is the one action that writes an image (to data/camera/), because
the user asked for a picture.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sugar.core.processes import CREATE_NO_WINDOW

log = logging.getLogger(__name__)


@dataclass
class CameraDevice:
    index: int
    name: str


def device_names() -> list[str]:
    """Friendly names of the cameras Windows knows about (PnP Camera/Image classes, present only)."""
    script = ("Get-PnpDevice -Class Camera,Image -PresentOnly -ErrorAction SilentlyContinue | "
              "Where-Object Status -eq 'OK' | ForEach-Object FriendlyName")
    try:
        output = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, timeout=15, creationflags=CREATE_NO_WINDOW).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


class Camera:
    """One camera at a time, opened on request and always released."""

    def __init__(self, *, index: int | None = None, folder: Path | None = None, idle_release_s: float = 120.0,
                 opener=None, on_change=None) -> None:
        self.preferred = index
        self.folder = folder
        self.idle_release_s = idle_release_s
        self._opener = opener  # (index) -> capture object with read()/release()/isOpened(); tests inject a fake
        self._on_change = on_change  # (active: bool, device: str | None) -> None, for the UI badge
        self._cap = None
        self._device: CameraDevice | None = None
        self._lock = threading.RLock()
        self._last_used = 0.0
        self._timer: threading.Timer | None = None
        self.last_frame_at: float | None = None

    # ------------------------------------------------------------------ discovery

    def _open_index(self, index: int):
        if self._opener is not None:
            return self._opener(index)
        import cv2

        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(index, cv2.CAP_MSMF)
        return cap

    def discover(self, probe: int = 4) -> list[CameraDevice]:
        names = device_names() if self._opener is None else []
        found = []
        with self._lock:
            if self._cap is not None:  # don't steal the open device to probe it
                return [self._device] if self._device else []
            for index in range(probe):
                cap = self._open_index(index)
                try:
                    if cap is not None and cap.isOpened():
                        found.append(CameraDevice(index, names[index] if index < len(names) else f"Camera {index}"))
                finally:
                    if cap is not None:
                        cap.release()
        return found

    # ------------------------------------------------------------------ lifecycle

    @property
    def active(self) -> bool:
        return self._cap is not None

    @property
    def device(self) -> CameraDevice | None:
        return self._device

    def open(self) -> CameraDevice:
        with self._lock:
            if self._cap is not None and self._device is not None:
                self._touch()
                return self._device
            order = [self.preferred] if self.preferred is not None else []
            order += [i for i in range(4) if i not in order]
            names = device_names() if self._opener is None else []
            for index in order:
                cap = self._open_index(index)
                if cap is not None and cap.isOpened():
                    self._cap = cap
                    self._device = CameraDevice(index, names[index] if index < len(names) else f"Camera {index}")
                    break
                if cap is not None:
                    cap.release()
            if self._cap is None:
                raise RuntimeError("no camera could be opened (is another app using it, or is camera access "
                                   "turned off in Windows privacy settings?)")
            self._touch()
            log.info("camera on: %s", self._device.name)
            if self._on_change:
                self._on_change(True, self._device.name)
            return self._device

    def _touch(self) -> None:
        self._last_used = time.monotonic()
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self.idle_release_s, self._idle_check)
        self._timer.daemon = True
        self._timer.start()

    def _idle_check(self) -> None:
        if self._cap is not None and time.monotonic() - self._last_used >= self.idle_release_s - 0.5:
            log.info("camera released after %.0f s without use", self.idle_release_s)
            self.close()

    def frame(self, warmup: int = 5):
        """A fresh frame as a PIL image (RGB). Opens the camera if needed."""
        from PIL import Image

        with self._lock:
            self.open()
            frame = None
            for _ in range(max(1, warmup)):  # exposure and white balance settle over the first frames
                ok, candidate = self._cap.read()
                if ok and candidate is not None:
                    frame = candidate
            if frame is None:
                raise RuntimeError("the camera returned no picture")
            self._touch()
            self.last_frame_at = time.time()
        rgb = frame[:, :, ::-1] if getattr(frame, "ndim", 0) == 3 else frame
        return Image.fromarray(rgb)

    def photo(self) -> Path:
        image = self.frame()
        folder = self.folder or Path("data/camera")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"photo-{datetime.now():%Y%m%d-%H%M%S}.jpg"
        image.save(path, quality=92)
        return path

    def close(self) -> bool:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            cap, self._cap = self._cap, None
            device, self._device = self._device, None
            if cap is None:
                return False
            try:
                cap.release()
            finally:
                log.info("camera off: %s", device.name if device else "?")
                if self._on_change:
                    self._on_change(False, None)
            return True
