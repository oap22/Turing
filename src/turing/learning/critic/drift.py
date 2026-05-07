"""DriftDetector — sliding-window comparison between local and cloud critic scores.

The local 13B+ critic does the bulk of scoring; ~5% of episodes are also
scored by Claude Haiku as a calibration check. When the rolling mean absolute
error between the two exceeds `threshold`, the local critic has drifted and
needs recalibration. The detector fires `on_drift(observed_mae)` exactly once
per drift crossing — re-arms only after a window's worth of new agreement.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class DriftDetector:
    def __init__(
        self,
        *,
        window: int,
        threshold: float,
        on_drift: Callable[[float], None] | None = None,
    ) -> None:
        if window <= 0:
            raise ValueError("window must be positive")
        self._window: deque[float] = deque(maxlen=window)
        self._threshold = threshold
        self._on_drift = on_drift
        self._fired = False

    def observe(self, *, local: float, cloud: float) -> None:
        self._window.append(abs(local - cloud))
        if len(self._window) < self._window.maxlen:  # type: ignore[operator]
            return

        mae = sum(self._window) / len(self._window)
        drifting = mae > self._threshold
        if drifting and not self._fired:
            self._fired = True
            if self._on_drift is not None:
                self._on_drift(mae)
        elif not drifting and self._fired:
            self._fired = False

    def is_drifting(self) -> bool:
        if not self._window:
            return False
        mae = sum(self._window) / len(self._window)
        return mae > self._threshold
