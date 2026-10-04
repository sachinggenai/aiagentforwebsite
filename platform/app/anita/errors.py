"""Exception types shared across the boot path.

All three carry the *key or resource name*, never a value — a config error must
be diagnosable from the log without leaking a secret (TRD-44).
"""

from __future__ import annotations


class ConfigError(Exception):
    """A configuration value is missing, malformed, or fails its type check.

    Names the offending key so an operator can fix it without reading source.
    """

    def __init__(self, key: str, reason: str) -> None:
        self.key = key
        self.reason = reason
        super().__init__(f"{key}: {reason}")


class IsolationViolation(Exception):
    """An ANITA resource collides with the sibling university deployment.

    Carrying both values makes the message actionable: the operator sees what
    ANITA is set to *and* what the sibling already owns (TRD-42).
    """

    def __init__(self, resource: str, anita_value: object, sibling_value: object, action: str) -> None:
        self.resource = resource
        self.anita_value = anita_value
        self.sibling_value = sibling_value
        self.action = action
        super().__init__(self.render())

    def render(self) -> str:
        return (
            "BOOT REFUSED — isolation collision (BRD-18)\n"
            f"  resource : {self.resource}\n"
            f"  anita    : {self.anita_value}\n"
            f"  sibling  : {self.sibling_value}\n"
            f"  action   : {self.action}"
        )


class SecretMissing(Exception):
    """A required secret is not set. Reports presence only, never a value."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"BOOT REFUSED — required secret is unset: {key}")
