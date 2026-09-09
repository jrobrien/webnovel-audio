from __future__ import annotations

from typing import Protocol

import numpy as np

from ..segment import Segment


class Synthesizer(Protocol):
    sample_rate: int

    def synth(self, seg: Segment) -> np.ndarray:  # float32 mono, [-1, 1]
        ...

    def close(self) -> None:
        ...
