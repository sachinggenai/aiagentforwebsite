"""Platform detection and model-backend selection (TRD-43, BRD-19).

The platform branch is **derived, never hand-set**. No configuration file
differs between operating systems, and an operator never edits an OS-specific
value to make the stack start. That property is what makes "supports all
platforms" true rather than aspirational — a documented procedure that someone
must remember is a procedure that will be forgotten on the second deployment.

Probe order is CUDA → Metal → CPU. CPU is a valid *detection* result but not a
usable voice deployment: the degradation ladder (US-018) refuses voice rather
than serving it at unusable latency.
"""

from __future__ import annotations

import platform
import shutil
import sys
from dataclasses import dataclass

from .config import AnitaSettings


@dataclass(frozen=True)
class DeviceConfig:
    """What this machine can actually do."""

    device: str          # cuda | metal | cpu
    compute_type: str    # int8 | float32 | int8_float16
    dtype: str           # float16 | float32
    total_memory_gb: float
    vendor: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class ModelConfig:
    """Which backend and models this platform should use."""

    backend: str          # ollama | mlx
    chat_model: str
    base_url: str
    embed_model: str
    keep_alive: str
    voice_capable: bool
    reason: str = ""


def _cuda_available() -> tuple[bool, float, str]:
    """Probe for an NVIDIA GPU without importing torch unless it is installed.

    torch is a heavy import and is not needed for chat-only deployments, so the
    probe degrades to a cheap nvidia-smi check when torch is absent.
    """
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            return True, props.total_memory / (1024**3), props.name
        return False, 0.0, "torch reports no CUDA device"
    except ImportError:
        pass

    if shutil.which("nvidia-smi"):
        # Present but torch-less: report the device so the operator gets a
        # useful message instead of a silent CPU downgrade.
        return False, 0.0, "nvidia-smi present but torch is not installed"

    return False, 0.0, "no CUDA runtime found"


def _metal_available() -> bool:
    return sys.platform == "darwin" and platform.machine().lower() in {"arm64", "aarch64"}


def detect_platform() -> DeviceConfig:
    """Probe the machine in order: CUDA, then Metal, then CPU."""
    has_cuda, vram_gb, detail = _cuda_available()
    if has_cuda:
        return DeviceConfig(
            device="cuda",
            compute_type="int8",       # CTranslate2 whisper on GPU
            dtype="float16",
            total_memory_gb=round(vram_gb, 1),
            vendor="nvidia",
            reason=f"CUDA device detected ({detail})",
        )

    if _metal_available():
        return DeviceConfig(
            device="metal",
            # CTranslate2 has no Metal backend; faster-whisper runs on CPU with
            # int8 while the LLM uses MLX on the GPU.
            compute_type="int8",
            dtype="float16",
            total_memory_gb=0.0,
            vendor="apple",
            reason="Apple Silicon detected (LLM via MLX, speech on CPU/int8)",
        )

    return DeviceConfig(
        device="cpu",
        compute_type="float32",
        dtype="float32",
        total_memory_gb=0.0,
        vendor=None,
        reason=detail or "no accelerator detected",
    )


def pick_model(device: DeviceConfig, settings: AnitaSettings) -> ModelConfig:
    """Map a detected platform to a backend with no operator input.

    Mirrors the source system's `pick_model()` (REC-08) so the same deployment
    procedure works on a Windows GPU box and an Apple laptop.
    """
    configured = (settings.model.llm_provider or "auto").lower()

    if configured == "ollama" or (configured == "auto" and device.device in {"cuda", "cpu"}):
        return ModelConfig(
            backend="ollama",
            chat_model=settings.model.ollama_model,
            base_url=settings.model.ollama_url,
            embed_model=settings.model.embed_model,
            keep_alive=settings.model.ollama_keep_alive,
            # CPU-only is detected but not voice-capable: serving calls at CPU
            # TTS rates would breach the latency SLO rather than miss it
            # slightly (06-architecture.md §5).
            voice_capable=device.device != "cpu",
            reason=f"device={device.device}",
        )

    if configured == "mlx" or (configured == "auto" and device.device == "metal"):
        return ModelConfig(
            backend="mlx",
            chat_model=settings.model.mlx_model,
            base_url=settings.model.mlx_base_url,
            embed_model=settings.model.embed_model,
            keep_alive=settings.model.ollama_keep_alive,
            voice_capable=True,
            reason=f"device={device.device}",
        )

    # Explicitly configured to something we do not recognise — fail loudly
    # rather than silently falling back to a backend the operator did not ask for.
    raise ValueError(
        f"ANITA_LLM_PROVIDER={configured!r} is not a known provider "
        f"(expected auto, ollama or mlx)"
    )
