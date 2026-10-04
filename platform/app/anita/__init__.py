"""ANITA MEDICAL SYSTEMS — product marketing & support platform.

Built by re-homing the multi-channel voice/chat platform from the Meridian
admissions assistant onto the ANITA medical-equipment catalogue, with its own
isolated database, vector index and model configuration.

This package currently implements **MOD-09 — Configuration, Isolation & Boot**,
which is deliberately first: the isolation guard is what prevents this instance
and the sibling university deployment from silently serving each other's data,
and retrofitting it later would invalidate every test written before it.

See `docs/sdlc/` for the full plan.
"""

__version__ = "0.1.0"

__all__ = [
    "boot",
    "config",
    "config_truth",
    "errors",
    "isolation",
    "platform_detect",
    "secrets",
]
