"""Secret classification and masking.

Carried from the source system's `config_truth` masking rule (REC-14), which is
one of the genuinely good ideas in that codebase: a key that *looks* like a
secret is masked everywhere by default, and only its presence is ever reported.

This is why no credential value appears anywhere in the plan artifacts or in
any boot output — masking is the mechanism, not a convention someone remembers.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

# The marker list is deliberately broad. A false positive costs a masked value
# in a log line; a false negative leaks a credential. Asymmetric by design.
SENSITIVE_MARKERS: tuple[str, ...] = (
    "TOKEN", "SECRET", "PASSWORD", "PASSWD", "PASS",
    "KEY", "CREDENTIAL", "SID", "AUTH",
)

_SENSITIVE_RE = re.compile("|".join(SENSITIVE_MARKERS), re.IGNORECASE)

MASK = "***"

# Presence values. Deliberately not booleans — "set"/"unset" reads correctly in
# a log line and cannot be mistaken for the value itself.
SET = "set"
UNSET = "unset"


def is_sensitive(key: str) -> bool:
    """True when a key name marks it as carrying a secret value."""
    return bool(_SENSITIVE_RE.search(key or ""))


def mask_value(key: str, value: Any) -> str:
    """Render one value for output, masking it when the key is sensitive.

    Non-sensitive values are rendered as-is so the boot report stays useful.
    """
    if is_sensitive(key):
        return SET if value not in (None, "") else UNSET
    if value is None:
        return ""
    return str(value)


def mask_mapping(values: Mapping[str, Any]) -> dict[str, str]:
    """Mask an entire mapping, returning key -> safe-to-print value."""
    return {k: mask_value(k, v) for k, v in values.items()}


def redact_text(text: str, secrets: Mapping[str, Any] | None = None) -> str:
    """Replace any secret *value* that leaked into free text with the mask.

    Belt-and-braces for error messages that interpolate an unexpected object.
    Only non-empty values are redacted; masking an empty string would corrupt
    unrelated text.
    """
    if not text or not secrets:
        return text
    redacted = text
    for key, value in secrets.items():
        if not is_sensitive(key):
            continue
        if value in (None, ""):
            continue
        redacted = redacted.replace(str(value), MASK)
    return redacted


def presence(key: str, value: Any) -> str:
    """Return ``set`` / ``unset`` for a key — the only fact about a secret
    this platform ever reports."""
    return SET if value not in (None, "") else UNSET
