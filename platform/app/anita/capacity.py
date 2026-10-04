"""Capacity: the VRAM pressure sampler and the degradation policy (US-018).

The ladder, the ceiling and the footprint maths already existed in
`speech.AdmissionController` — and **nothing drove them**. The audit found
`.degrade(` had no caller outside tests and `gpu_free_mib` was registered
unavailable and never written, so the product could not shed voice under
pressure at all; and the boot report's "voice will be refused at admission"
on a host without a GPU was a claim no code implemented.

This module is the missing driver:

* `nvidia_smi_free_mib` — the pressure source. `None` means *not measurable*,
  and the policy then does nothing rather than guessing (a fabricated reading
  would degrade a healthy box or, worse, hold a struggling one together).
* `CapacityPolicy` — one sample in, at most one ladder transition out. The
  order is the story's `voice_shed → stt_off → tts_off → chat_degrade →
  commercial_only`, which is the existing `LADDER`: voice is shed before any
  text capability is touched.
* `CapacityMonitor` — samples on an interval, publishes `gpu_free_mib` to the
  metrics registry, and applies the policy. Started at boot only when there is
  a device to read.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from typing import Callable, Protocol

from .speech import LADDER, AdmissionController, DegradationLevel

logger = logging.getLogger("anita.capacity")

SAMPLE_SECONDS = 5.0
#: Free memory must exceed what the next rung needs *plus* the sibling's floor
#: by this much before a rung is recovered. Without hysteresis a box hovering
#: at the threshold flaps between rungs, and every flap logs a transition.
HYSTERESIS_MIB = 512


class PressureSource(Protocol):
    def __call__(self) -> int | None: ...


def nvidia_smi_free_mib() -> int | None:
    """Free memory on device 0, in MiB — or None when it cannot be read.

    nvidia-smi rather than a CUDA binding: it is present wherever a driver is,
    needs no import, and this probe runs on a timer. A machine without it
    simply has no GPU to measure.
    """
    if not shutil.which("nvidia-smi"):
        return None
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        first = (completed.stdout or "").strip().splitlines()[0]
        return int(float(first.strip()))
    except Exception:  # noqa: BLE001 - an unreadable probe is absence, not zero
        logger.debug("nvidia-smi free-memory probe failed", exc_info=True)
        return None


class CapacityPolicy:
    """One sample in, at most one ladder transition out.

    Down is driven by the *sibling's floor*, not by ANITA's own footprint: the
    card is shared with a product that is not ours, and the moment its floor is
    threatened is the moment to shed — waiting until ANITA itself is short
    means the damage is already done to the other product.
    """

    def __init__(
        self,
        controller: AdmissionController,
        *,
        source: PressureSource,
        hysteresis_mib: int = HYSTERESIS_MIB,
    ) -> None:
        self.controller = controller
        self.source = source
        self.hysteresis_mib = hysteresis_mib
        self.transitions: list[tuple[str, str]] = []

    def sample(self) -> str | None:
        """Read the device and move at most one rung. Returns the move made."""
        free = self.source()
        if free is None:
            return None  # not measurable: never guess
        return self.sample_from(free)

    def sample_from(self, free_mib: int) -> str | None:
        """Apply a reading already taken.

        Split from `sample` so the monitor can publish the metric and make the
        decision from the *same* reading — fetching twice would let the number
        on the dashboard and the number the policy acted on disagree.
        """
        floor = self.controller.sibling_floor_mib
        if free_mib < floor:
            level = self.controller.degrade(
                f"device has {free_mib} MiB free, below the sibling's {floor} MiB floor"
            )
            return self._record("degrade", level)

        # Recovery is deliberately harder than degradation — it needs the
        # footprint of the rung above, the sibling's floor, and the margin.
        index = LADDER.index(self.controller.level)
        if index == 0:
            return None
        above = LADDER[index - 1]
        needed = self.controller.footprint_mib(above) + floor + self.hysteresis_mib
        if free_mib >= needed:
            level = self.controller.recover()
            return self._record("recover", level)
        return None

    def _record(self, event: str, level: DegradationLevel) -> str:
        detail = f"{event}:{level.value}"
        self.transitions.append((event, level.value))
        logger.warning("capacity %s → %s", event, level.value)
        return detail


class CapacityMonitor:
    """Samples on an interval and keeps `gpu_free_mib` honest.

    The metric is the point of the sampler even when nothing degrades: an
    operator needs to see pressure *before* it crosses a threshold, and a
    metric that is only written when something goes wrong is a metric nobody
    can act on.
    """

    def __init__(
        self,
        policy: CapacityPolicy,
        *,
        metrics=None,
        source: PressureSource | None = None,
        sample_s: float = SAMPLE_SECONDS,
    ) -> None:
        self.policy = policy
        self.metrics = metrics
        self.source = source or policy.source
        self.sample_s = sample_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- one tick, used by the thread and by tests -------------------------
    def tick(self) -> str | None:
        free = self.source()
        if free is None:
            if self.metrics is not None:
                self.metrics.mark_unavailable("gpu_free_mib", "MiB")
            return None
        if self.metrics is not None:
            self.metrics.set("gpu_free_mib", free, "MiB")
        return self.policy.sample_from(free)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="anita-vram-monitor", daemon=True)
        self._thread.start()
        logger.info("capacity monitor started (every %.0fs)", self.sample_s)

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.wait(self.sample_s):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a monitor must never take the app down
                logger.exception("capacity sample failed")


__all__ = [
    "CapacityMonitor",
    "CapacityPolicy",
    "PressureSource",
    "nvidia_smi_free_mib",
]
