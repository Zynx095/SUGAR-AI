"""Continuous microphone capture.

The PortAudio callback only copies samples into a bounded queue; all
processing happens on the audio front-end thread. If the device cannot be
opened at 16 kHz the stream runs at its native rate and is resampled with
soxr. Frames are always exactly ``block_size`` samples of float32 mono.

Disconnects and device changes are handled by :meth:`MicrophoneStream.restart`,
which re-initialises PortAudio so newly plugged devices become visible.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)


def list_input_devices() -> list[dict]:
    devices = []
    for index, device in enumerate(sd.query_devices()):
        if device["max_input_channels"] > 0:
            devices.append({"index": index, "name": device["name"], "hostapi": device["hostapi"],
                            "default_samplerate": device["default_samplerate"]})
    return devices


def resolve_device(spec: str | int | None, kind: str) -> int | None:
    """Turn a device index or name fragment into a PortAudio index (None = default)."""
    if spec is None or spec == "":
        return None
    if isinstance(spec, int):
        return spec
    channels_key = "max_input_channels" if kind == "input" else "max_output_channels"
    wanted = str(spec).lower()
    default_api = sd.default.hostapi
    matches = [
        (index, device)
        for index, device in enumerate(sd.query_devices())
        if device[channels_key] > 0 and wanted in device["name"].lower()
    ]
    if not matches:
        log.warning("no %s device matches %r; using the system default", kind, spec)
        return None
    matches.sort(key=lambda item: item[1]["hostapi"] != default_api)
    return matches[0][0]


class MicrophoneStream:
    def __init__(self, device: str | int | None = None, sample_rate: int = 16000, block_size: int = 512) -> None:
        self._device_spec = device
        self.sample_rate = sample_rate
        self.block_size = block_size
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=256)  # ~8 s of audio
        self._stream: sd.InputStream | None = None
        self._resampler = None
        self._pending = np.zeros(0, dtype=np.float32)
        self._lock = threading.Lock()
        self.device_name: str | None = None
        self.overflows = 0
        self.dropped = 0
        self.last_frame_time = 0.0

    @property
    def active(self) -> bool:
        stream = self._stream
        return stream is not None and stream.active

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                return
            index = resolve_device(self._device_spec, "input")
            info = sd.query_devices(index if index is not None else sd.default.device[0], "input")
            self.device_name = info["name"]
            try:
                self._open(index, self.sample_rate)
                self._resampler = None
            except sd.PortAudioError:
                native = int(info["default_samplerate"])
                import soxr

                log.info("device %s rejects %d Hz; capturing at %d Hz and resampling", self.device_name,
                         self.sample_rate, native)
                self._resampler = soxr.ResampleStream(native, self.sample_rate, 1, dtype="float32")
                self._open(index, native)
            self.last_frame_time = time.monotonic()
            log.info("microphone open: %s", self.device_name)

    def _open(self, index: int | None, rate: int) -> None:
        blocksize = self.block_size if rate == self.sample_rate else 0
        stream = sd.InputStream(
            device=index,
            samplerate=rate,
            channels=1,
            dtype="float32",
            blocksize=blocksize,
            latency="low",
            callback=self._callback,
        )
        stream.start()
        self._stream = stream

    def _callback(self, indata: np.ndarray, frames: int, time_info, status: sd.CallbackFlags) -> None:
        if status.input_overflow:
            self.overflows += 1
        samples = indata[:, 0].astype(np.float32, copy=True)
        if self._resampler is not None:
            samples = self._resampler.resample_chunk(samples)
        if samples.size == self.block_size and self._pending.size == 0:
            self._push(samples)
            return
        buffer = np.concatenate([self._pending, samples]) if self._pending.size else samples
        count = buffer.size // self.block_size
        for i in range(count):
            self._push(buffer[i * self.block_size:(i + 1) * self.block_size])
        self._pending = buffer[count * self.block_size:].copy()

    def _push(self, frame: np.ndarray) -> None:
        self.last_frame_time = time.monotonic()
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            # The consumer stalled: drop the oldest frame, keep the newest.
            self.dropped += 1
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(frame)
            except (queue.Empty, queue.Full):
                pass

    def read(self, timeout: float = 0.5) -> np.ndarray | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def flush(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def stop(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
            self._pending = np.zeros(0, dtype=np.float32)
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except sd.PortAudioError:
                pass

    def restart(self) -> None:
        """Re-open the device after a disconnect or a default-device change."""
        self.stop()
        try:
            sd._terminate()
            sd._initialize()
        except Exception:  # PortAudio re-init is best effort
            log.debug("PortAudio re-initialisation failed", exc_info=True)
        self.start()

    def stalled(self, threshold_s: float = 2.0) -> bool:
        return self._stream is not None and time.monotonic() - self.last_frame_time > threshold_s
