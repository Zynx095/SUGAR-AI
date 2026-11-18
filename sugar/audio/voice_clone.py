"""Voice cloning: MeloTTS speech converted to the timbre of a reference recording.

OpenVoice V2's tone colour converter (MyShell, MIT) re-voices audio from one speaker embedding to
another while keeping the words, timing and intonation. SID synthesises with MeloTTS ``EN-Newest``
(OpenVoice ships that voice's embedding) and converts it to the embedding extracted from
``data/voice_samples/ULTRON_SID.mp3``. The converter runs on the GPU in a few tens of milliseconds
per sentence.

Files (all on D:, nothing on C:):
    D:/Sugar_Models/OpenVoiceV2/converter/{config.json,checkpoint.pth}
    D:/Sugar_Models/OpenVoiceV2/base_speakers/ses/en-newest.pth
    D:/Sugar_Models/OpenVoiceV2/targets/<reference>-<mtime>.pth   (cached target embedding)
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


class _HParams:
    """Attribute/mapping view of the converter config (what SynthesizerTrn expects as **kwargs)."""

    def __init__(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, _HParams(**value) if isinstance(value, dict) else value)

    def keys(self):
        return self.__dict__.keys()

    def __getitem__(self, key):
        return getattr(self, key)


class ToneConverter:
    def __init__(self, models_dir: Path, reference: Path, *, source: str = "en-newest", tau: float = 0.3,
                 device: str = "auto") -> None:
        self.models_dir = Path(models_dir)
        self.reference = Path(reference)
        self.source = source
        self.tau = tau
        self._device_pref = device
        self.device = "cpu"
        self.sample_rate = 22050
        self._model = None
        self._hps = None
        self._src = None
        self._tgt = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return ((self.models_dir / "converter" / "checkpoint.pth").exists()
                and (self.models_dir / "base_speakers" / "ses" / f"{self.source}.pth").exists()
                and self.reference.exists())

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        import torch

        from openvoice.models import SynthesizerTrn

        if not self.available:
            raise FileNotFoundError(f"voice clone files missing (models in {self.models_dir}, "
                                    f"reference {self.reference})")
        self.device = "cuda" if self._device_pref != "cpu" and torch.cuda.is_available() else "cpu"
        config = json.loads((self.models_dir / "converter" / "config.json").read_text(encoding="utf-8"))
        hps = _HParams(**config)
        model = SynthesizerTrn(0, hps.data.filter_length // 2 + 1, n_speakers=hps.data.n_speakers, **hps.model)
        checkpoint = torch.load(self.models_dir / "converter" / "checkpoint.pth", map_location=self.device,
                                weights_only=False)
        missing, unexpected = model.load_state_dict(checkpoint["model"], strict=False)
        if missing:
            log.warning("voice converter: %d missing weights", len(missing))
        self._model = model.to(self.device).eval()
        self._hps = hps
        self.sample_rate = int(hps.data.sampling_rate)
        self._src = torch.load(self.models_dir / "base_speakers" / "ses" / f"{self.source}.pth",
                               map_location=self.device, weights_only=False).to(self.device)
        self._tgt = self._target_embedding()
        log.info("voice converter ready on %s (reference %s)", self.device, self.reference.name)

    def _spectrogram(self, audio: np.ndarray):
        import torch

        from openvoice.mel_processing import spectrogram_torch

        hps = self._hps
        y = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32)).to(self.device).unsqueeze(0)
        return spectrogram_torch(y, hps.data.filter_length, hps.data.sampling_rate, hps.data.hop_length,
                                 hps.data.win_length, center=False).to(self.device)

    def _target_embedding(self):
        import librosa
        import torch

        cache = self.models_dir / "targets" / f"{self.reference.stem}-{int(self.reference.stat().st_mtime)}.pth"
        if cache.exists():
            return torch.load(cache, map_location=self.device, weights_only=False).to(self.device)
        audio, _ = librosa.load(str(self.reference), sr=self.sample_rate, mono=True)
        with torch.no_grad():
            g = self._model.ref_enc(self._spectrogram(audio).transpose(1, 2)).unsqueeze(-1)
        cache.parent.mkdir(parents=True, exist_ok=True)
        torch.save(g.detach().cpu(), cache)
        return g.detach()

    def convert(self, audio: np.ndarray, rate: int, air_db: float | None = None) -> np.ndarray:
        """Re-voice ``audio`` (float32 mono at ``rate``); returns float32 at ``rate``.

        The converter works at 22.05 kHz, so it returns nothing above ~11 kHz. With ``air_db`` set, the
        treble above the crossover is taken from the original synthesis (sibilance and breath, which carry
        little speaker identity), scaled by ``air_db``, so the result keeps a full-band top end.
        """
        import soxr
        import torch

        if self._model is None:
            raise RuntimeError("voice converter is not loaded")
        if audio.size == 0:
            return audio
        source = soxr.resample(audio.astype(np.float32), rate, self.sample_rate).astype(np.float32)
        with self._lock, torch.inference_mode():
            spec = self._spectrogram(source)
            lengths = torch.LongTensor([spec.size(-1)]).to(self.device)
            out = self._model.voice_conversion(spec, lengths, sid_src=self._src, sid_tgt=self._tgt,
                                               tau=self.tau)[0][0, 0]
            converted = out.float().cpu().numpy()
        result = soxr.resample(converted, self.sample_rate, rate).astype(np.float32)
        if air_db is None or rate <= 2 * CROSSOVER_HZ:
            return result
        from scipy.signal import butter, sosfiltfilt

        n = min(result.size, audio.size)
        low = sosfiltfilt(butter(8, CROSSOVER_HZ, "lowpass", fs=rate, output="sos"), result[:n])
        high = sosfiltfilt(butter(8, CROSSOVER_HZ, "highpass", fs=rate, output="sos"), audio[:n])
        return (low + high * 10 ** (air_db / 20)).astype(np.float32)


CROSSOVER_HZ = 9500.0
