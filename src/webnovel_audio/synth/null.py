from __future__ import annotations

import numpy as np

from ..segment import Segment


class NullSynth:
    """Emits silence sized to an estimated speaking duration.

    Lets you exercise segmentation + assembly + loudnorm + packaging with no
    model files and no network. `render --backend null` should always work.
    """

    def __init__(self, sample_rate: int = 24000, words_per_second: float = 2.7):
        self.sample_rate = sample_rate
        self.fingerprint = f"null-{sample_rate}-{words_per_second:g}"
        self.wps = words_per_second

    def synth(self, seg: Segment) -> np.ndarray:
        words = max(1, len(seg.text.split()))
        seconds = words / self.wps / max(seg.rate, 0.1)
        return np.zeros(int(seconds * self.sample_rate), dtype="float32")

    def close(self) -> None:
        pass
