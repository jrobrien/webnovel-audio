from __future__ import annotations

from typing import Protocol

import numpy as np

from ..segment import Segment


class Synthesizer(Protocol):
    sample_rate: int
    #: short id for "what produces this audio" — model bytes, voice embeddings,
    #: language and g2p chain. Segments are cached under it, so a model or
    #: espeak bump starts a new generation instead of silently reusing the old.
    fingerprint: str

    def synth(self, seg: Segment) -> np.ndarray:  # float32 mono, [-1, 1]
        ...

    def close(self) -> None:
        ...
