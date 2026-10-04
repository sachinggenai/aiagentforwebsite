"""Isolation assertions — the guard that refuses a colliding boot (TRD-42).

Why this module exists as its own thing, rather than as a comment saying "use a
different database":

**The failure it prevents is silent.** If ANITA pointed at the university's
database, collection, port or log directory, every service would start, every
health check would pass, and the product would quietly serve the wrong answers
and overwrite the wrong leads. Nothing would crash. A crash is *better* than
this bug — so the guard turns it into a crash, deliberately, at boot.

Per `CV-01`, isolation is **logical, not physical**: the Twilio account and the
GPU are explicitly shared resources and are not checked here. What is checked is
that this instance owns its own database, its own vector collection, its own
ports and its own log directory.

The guard runs **before** the app object is created, before any port is bound
and before any database connection is opened (`M9-6`). A refusal therefore
leaves no partial state: no socket, no connection, nothing to clean up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import AnitaSettings, ForbiddenValues
from .errors import IsolationViolation

# Actions are phrased as instructions, not diagnoses: an operator reading a
# refused boot at 2am needs to know what to type, not what went wrong
# philosophically.
ACTION_DB = "set ANITA_DB_NAME to a database unique to this product (e.g. anita_ams)"
ACTION_SCHEMA = (
    "set ANITA_DB_SCHEMA to a schema unique to this product (e.g. anita); "
    "sharing a database with the sibling product is not isolation"
)
ACTION_COLLECTION = "set ANITA_CHROMA_COLLECTION to a value unique to this product (e.g. anita_ams__v1)"
ACTION_PORT = "choose a port not used by the sibling product (ANITA uses 8100/8601/8602/8085)"
ACTION_LOG_DIR = "set ANITA_LOG_DIR to a directory scoped to this instance (e.g. ./logs/anita)"


@dataclass
class IsolationCheck:
    """One resource comparison, kept as data so the preflight can report all
    failures rather than stopping at the first."""

    resource: str
    anita_value: object
    passed: bool
    sibling_value: object | None = None
    action: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict:
        return {
            "resource": self.resource,
            "anita": self.anita_value,
            "sibling": self.sibling_value,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass
class IsolationVerdict:
    """The full result of running the guard."""

    checks: list[IsolationCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failures(self) -> list[IsolationCheck]:
        return [check for check in self.checks if not check.passed]

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checks": [check.as_dict() for check in self.checks],
            "failures": [check.resource for check in self.failures],
        }


def _check_db_name(settings: AnitaSettings, forbidden: ForbiddenValues) -> IsolationCheck:
    value = settings.isolation.db_name
    if value in forbidden.db_name:
        return IsolationCheck("db_name", value, False, value, ACTION_DB)
    return IsolationCheck("db_name", value, True)


def _check_db_schema(settings: AnitaSettings) -> IsolationCheck:
    """A schema equal to the database name is almost always a copy-paste slip,
    and on a shared cluster it silently points two products at one namespace."""
    schema = settings.isolation.db_schema
    name = settings.isolation.db_name
    if schema == name:
        return IsolationCheck(
            "db_schema",
            schema,
            False,
            None,
            ACTION_SCHEMA,
            detail="schema equals database name",
        )
    return IsolationCheck("db_schema", schema, True)


def _check_collection(settings: AnitaSettings, forbidden: ForbiddenValues) -> IsolationCheck:
    value = settings.isolation.chroma_collection
    if value in forbidden.chroma_collection:
        return IsolationCheck("chroma_collection", value, False, value, ACTION_COLLECTION)
    return IsolationCheck("chroma_collection", value, True)


def _check_ports(settings: AnitaSettings, forbidden: ForbiddenValues) -> list[IsolationCheck]:
    checks: list[IsolationCheck] = []
    for label, port in (
        ("api_port", settings.isolation.api_port),
        ("chat_port", settings.isolation.chat_port),
        ("admin_port", settings.isolation.admin_port),
        ("streamlit_port", settings.isolation.streamlit_port),
    ):
        if port in forbidden.port:
            checks.append(IsolationCheck(f"port:{label}", port, False, port, ACTION_PORT))
        else:
            checks.append(IsolationCheck(f"port:{label}", port, True))

    # An instance colliding with itself is also a collision.
    ports = settings.isolation.ports
    if len(set(ports)) != len(ports):
        checks.append(
            IsolationCheck(
                "port:internal",
                ports,
                False,
                None,
                "give the API, chat and admin surfaces distinct ports",
                detail="two ANITA surfaces share a port",
            )
        )
    return checks


def _check_log_dir(settings: AnitaSettings, forbidden: ForbiddenValues) -> IsolationCheck:
    """Refuse a log directory that is not scoped to this instance.

    Accepting a relative path (resolved against the project root) but refusing
    the shared root directory is the pragmatic reading of "not
    instance-scoped": the source system's launchers hard-coded a shared
    ``logs/`` path, which is exactly how one product's telemetry ends up
    entangled with another's.
    """
    raw = settings.isolation.log_dir
    resolved = settings.log_dir_path

    if raw in forbidden.log_dir:
        return IsolationCheck("log_dir", raw, False, raw, ACTION_LOG_DIR)

    shared_roots = {settings.project_root.resolve(), (settings.project_root / "logs").resolve()}
    if resolved in shared_roots:
        return IsolationCheck(
            "log_dir",
            raw,
            False,
            str(settings.project_root / "logs"),
            ACTION_LOG_DIR,
            detail="log directory is not scoped to this instance",
        )
    return IsolationCheck("log_dir", raw, True)


def evaluate_isolation(
    settings: AnitaSettings, forbidden: ForbiddenValues | None = None
) -> IsolationVerdict:
    """Run every isolation check and return the full verdict.

    Prefer this over :func:`assert_isolation` when you want to report *all*
    problems at once — which is what the preflight does, because making an
    operator fix four collisions one boot at a time is unkind.
    """
    forb = forbidden or settings.forbidden
    checks: list[IsolationCheck] = [
        _check_db_name(settings, forb),
        _check_db_schema(settings),
        _check_collection(settings, forb),
        *_check_ports(settings, forb),
        _check_log_dir(settings, forb),
    ]
    return IsolationVerdict(checks=checks)


def assert_isolation(
    settings: AnitaSettings, forbidden: ForbiddenValues | None = None
) -> IsolationVerdict:
    """Run the guard, raising :class:`IsolationViolation` on the first failure.

    The exception renders a machine-greppable, operator-readable block naming
    the resource, both values and the fix.
    """
    verdict = evaluate_isolation(settings, forbidden)
    for check in verdict.failures:
        raise IsolationViolation(
            resource=check.resource,
            anita_value=check.anita_value,
            sibling_value=check.sibling_value,
            action=check.action or "resolve the collision before booting",
        )
    return verdict


def assert_vram_budget(settings: AnitaSettings) -> None:
    """Refuse a VRAM budget that would starve the sibling product (TRD-43/AS-09).

    The card is shared. A budget that looks fine in isolation but leaves the
    university assistant below its floor is a boot refusal, not a warning: by
    the time it manifests, it manifests as the *other* product failing.
    """
    vram = settings.vram
    remaining = vram.shared_card_mib - vram.anita_budget_mib
    if remaining < vram.sibling_floor_mib:
        raise IsolationViolation(
            resource="vram_budget",
            anita_value=f"{vram.anita_budget_mib} MiB of {vram.shared_card_mib} MiB",
            sibling_value=f"would leave {remaining} MiB, floor is {vram.sibling_floor_mib} MiB",
            action="lower VRAM_ANITA_BUDGET_MIB so the sibling keeps its floor",
        )
