"""Boot preflight and start orchestration (TRD-44).

Ordered phases, each of which can refuse the boot:

``LOAD → AUDIT → ISOLATION → SECRETS → PLATFORM → DATA → WARM``

Two properties matter more than the phases themselves:

**All-or-nothing.** A refusal happens *before* the app object is created, before
any port is bound and before any connection is opened (``M9-6``). There is no
half-started stack to clean up, because nothing was started. The phases are
ordered so every cheap, purely-local check runs before anything with a side
effect.

**Preflight is side-effect-free and runs as its own process.** An operator can
validate a configuration change against a *live* instance without touching it —
if the new config is broken, the running instance never notices. That is the
difference between "we can change config safely" and "we can change config
during a maintenance window".

Run it:

    python -m anita.boot --preflight     # validate only; exit 0 / 2 / 3
    python -m anita.boot --start         # preflight, then start the stack
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import config_truth, platform_detect, secrets
from .config import AnitaSettings, load_settings
from .errors import ConfigError, IsolationViolation, SecretMissing
from .isolation import assert_isolation, assert_vram_budget, evaluate_isolation

# Exit codes, deliberately distinct so a launcher can branch without parsing text.
EXIT_OK = 0
EXIT_REFUSED = 2   # bootable config that violates a rule (isolation, inert key, …)
EXIT_CONFIG_ERROR = 3  # config could not be parsed at all

# Secrets the platform genuinely cannot start without. Presence is required;
# values are never read, rendered or logged (TRD-44).
REQUIRED_SECRETS: tuple[str, ...] = ("ANITA_ADMIN_TOKEN",)


@dataclass
class PhaseResult:
    name: str
    ok: bool
    detail: str = ""
    failures: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "phase": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "failures": self.failures,
        }


@dataclass
class PreflightReport:
    phases: list[PhaseResult] = field(default_factory=list)
    settings: AnitaSettings | None = None

    @property
    def ok(self) -> bool:
        return all(phase.ok for phase in self.phases)

    @property
    def failures(self) -> list[PhaseResult]:
        return [phase for phase in self.phases if not phase.ok]

    @property
    def exit_code(self) -> int:
        if self.ok:
            return EXIT_OK
        if any(phase.name == "LOAD" for phase in self.failures):
            return EXIT_CONFIG_ERROR
        return EXIT_REFUSED

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "phases": [phase.as_dict() for phase in self.phases],
        }


# --------------------------------------------------------------------------
# Phases
# --------------------------------------------------------------------------
def _phase_load(env, config_path, project_root) -> tuple[PhaseResult, AnitaSettings | None]:
    try:
        settings = load_settings(env=env, config_path=config_path, project_root=project_root)
    except ConfigError as exc:
        return PhaseResult("LOAD", False, f"config error in {exc.key}", [f"{exc.key}: {exc.reason}"]), None
    return PhaseResult("LOAD", True, f"config loaded from {config_path or 'default'}"), settings


def _phase_audit(settings: AnitaSettings) -> PhaseResult:
    """Refuse if any configured key is written but never read (DG-09)."""
    env_files = [p for p in (settings.project_root / ".env", settings.project_root / ".env.example") if p.exists()]
    report = config_truth.audit_config(
        env=settings.env,
        source_dir=Path(__file__).resolve().parent,
        env_files=env_files,
    )
    data = {
        "live": len(report.live),
        "inert": report.inert,
        "pending": report.pending,
        "pattern_inventory": report.pattern_inventory,
        "files_scanned": report.files_scanned,
    }
    if report.inert:
        # A key set for real that nothing reads is a defect: someone configured
        # something during an incident and it did nothing.
        return PhaseResult(
            "AUDIT",
            False,
            f"{len(report.inert)} key(s) set but never read",
            [f"INERT_KEY: {key}" for key in report.inert],
            data,
        )
    detail = f"{len(report.live)} key(s) live"
    if report.pending:
        # Not a failure: the template documents modules that have not landed.
        detail += f", {len(report.pending)} pending (modules not yet built)"
    return PhaseResult("AUDIT", True, detail, data=data)


def _phase_isolation(settings: AnitaSettings) -> PhaseResult:
    """Refuse any collision with the sibling deployment (BRD-18)."""
    try:
        assert_isolation(settings)
    except IsolationViolation as exc:
        return PhaseResult(
            "ISOLATION", False, "resource collision with the sibling product", [exc.render()]
        )
    try:
        assert_vram_budget(settings)
    except IsolationViolation as exc:
        return PhaseResult("ISOLATION", False, "VRAM budget starves the sibling", [exc.render()])

    verdict = evaluate_isolation(settings)
    iso = settings.isolation
    return PhaseResult(
        "ISOLATION",
        True,
        f"db={iso.db_name}/{iso.db_schema} collection={iso.chroma_collection} "
        f"ports={','.join(str(p) for p in iso.ports)}",
        data=verdict.as_dict(),
    )


def _phase_secrets(settings: AnitaSettings) -> PhaseResult:
    """Required secrets must be present. Presence only — never a value."""
    missing = [key for key in REQUIRED_SECRETS if not settings.env.get(key)]
    if missing:
        return PhaseResult(
            "SECRETS",
            False,
            f"{len(missing)} required secret(s) unset",
            [f"SECRET_UNSET: {key}" for key in missing],
        )
    return PhaseResult(
        "SECRETS",
        True,
        ", ".join(f"{key}={secrets.presence(key, settings.env.get(key))}" for key in REQUIRED_SECRETS),
    )


def _phase_platform(settings: AnitaSettings) -> PhaseResult:
    """Detect the platform and pick a backend — derived, never hand-set."""
    device = platform_detect.detect_platform()
    try:
        model = platform_detect.pick_model(device, settings)
    except ValueError as exc:
        return PhaseResult("PLATFORM", False, "model backend could not be selected", [str(exc)])

    # The reason is included because "device=cpu" on a machine with a GPU is
    # confusing without it — and the usual cause (torch not installed) is
    # something the operator can act on.
    detail = (
        f"device={device.device} backend={model.backend} model={model.chat_model} "
        f"voice_capable={model.voice_capable} — {device.reason}"
    )
    if not model.voice_capable:
        # Not a refusal: chat and WhatsApp work fine without a GPU, and voice
        # still works — it is **capped** at what one CPU can honour (US-018).
        # This used to say "voice will be refused at admission", which no code
        # implemented: a torch-less host admitted calls as if it had a GPU.
        from .speech import CPU_CONCURRENT_CALLS

        detail += f" (voice capped at {CPU_CONCURRENT_CALLS} concurrent call(s))"
    return PhaseResult(
        "PLATFORM",
        True,
        detail,
        data={
            "device": device.device,
            "reason": device.reason,
            "backend": model.backend,
            "chat_model": model.chat_model,
            "voice_capable": model.voice_capable,
        },
    )


def _phase_data(settings: AnitaSettings) -> PhaseResult:
    """The corpus must exist. Data absence is a refusal, not a warning: an
    answer engine with no catalogue can only invent."""
    corpus = settings.corpus_dir
    chunks = corpus / settings.corpus.chunks_file
    sqlite = corpus / settings.corpus.sqlite_file
    failures: list[str] = []

    if not corpus.exists():
        failures.append(f"DATA_MISSING: corpus directory {corpus} does not exist")
    else:
        if not chunks.exists():
            failures.append(f"DATA_MISSING: {chunks} does not exist")
        if not sqlite.exists():
            failures.append(f"DATA_MISSING: {sqlite} does not exist")

    if failures:
        return PhaseResult("DATA", False, "corpus not available", failures)

    # Report the actual size so the boot log answers "is this the corpus I think
    # it is?" without a second command.
    lines = 0
    try:
        with chunks.open(encoding="utf-8") as handle:
            lines = sum(1 for _ in handle if _.strip())
    except OSError:
        pass
    return PhaseResult("DATA", True, f"corpus ok ({lines} chunks)", data={"chunks": lines})


def _phase_index(settings: AnitaSettings) -> PhaseResult:
    """The index must exist before the platform serves (`SM-05`, US-010).

    `ABSENT` used to be a startup *note* — the platform booted, and every
    catalogue answer became a decline, which is a working product telling
    buyers it does not stock things it does stock. The story's edge case 1 is
    explicit that this refuses boot, and the refusal is only acceptable because
    it is actionable: the message names the command that fixes it.

    Read-only: it opens the build store only if the file already exists, and
    never creates one — a boot that writes the state it is checking would make
    a fresh deployment look like it had a build history.
    """
    from .index import live_collection_name

    chroma = (settings.project_root / settings.isolation.chroma_path).resolve()
    store_path = (
        (settings.project_root / settings.isolation.log_dir).resolve() / "anita_index.sqlite"
    )
    live = live_collection_name(store_path, "")
    has_vectors = chroma.exists() and any(chroma.iterdir())

    if live:
        return PhaseResult("INDEX", True, f"live collection {live}")
    if has_vectors:
        # Vectors without a promoted build: a pre-lifecycle index, still
        # serving. Allowed, and said out loud so "no build history" is not
        # mistaken for "no index".
        return PhaseResult(
            "INDEX", True,
            f"{settings.isolation.chroma_collection} present, no promoted build yet "
            f"(run scripts/rebuild_index.py to record one)",
        )

    return PhaseResult("INDEX", False, "no index", [
        "NO_INDEX: no built index and no promoted build for this instance.",
        "Run: .venv\\Scripts\\python scripts\\rebuild_index.py",
        "The platform refuses to serve an empty index: every catalogue answer "
        "would be a decline, and a working product would tell buyers it does "
        "not stock what it stocks.",
    ])


def _phase_warm(settings: AnitaSettings) -> PhaseResult:
    """Report backend reachability. Never blocks the boot on a cold model.

    Warming is genuinely slow (6–11 s for a 14B chat model), and a preflight
    that hangs is a preflight people stop running. The check reports reachability
    and defers residency to the start path.
    """
    device = platform_detect.detect_platform()
    try:
        model = platform_detect.pick_model(device, settings)
    except ValueError:
        return PhaseResult("WARM", True, "skipped (no backend selected)")

    reachable = False
    note = ""
    try:
        import httpx

        # A cheap, unauthenticated endpoint. Deliberately not a generation call.
        url = model.base_url.rstrip("/")
        probe = f"{url}/api/tags" if model.backend == "ollama" else f"{url}/v1/models"
        response = httpx.get(probe, timeout=2.0)
        reachable = response.status_code < 500
        note = f"HTTP {response.status_code}"
    except ImportError:
        note = "httpx not installed; reachability not checked"
        reachable = True
    except Exception as exc:  # noqa: BLE001 - any transport failure is "not up yet"
        note = f"{type(exc).__name__}"

    if not reachable:
        # A cold backend is not a config error. Report it; the start path warms it.
        return PhaseResult(
            "WARM", True, f"{model.backend} not reachable yet ({note}) — will warm on start"
        )
    return PhaseResult("WARM", True, f"{model.backend} reachable ({note})")


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------
def preflight(
    env=None,
    config_path: Path | str | None = None,
    project_root: Path | str | None = None,
) -> PreflightReport:
    """Run every phase, collecting all failures rather than stopping at the first."""
    report = PreflightReport()

    load_result, settings = _phase_load(env, config_path, project_root)
    report.phases.append(load_result)
    if settings is None:
        return report
    report.settings = settings

    report.phases.append(_phase_audit(settings))
    report.phases.append(_phase_isolation(settings))
    report.phases.append(_phase_secrets(settings))
    report.phases.append(_phase_platform(settings))
    report.phases.append(_phase_data(settings))
    report.phases.append(_phase_index(settings))
    report.phases.append(_phase_warm(settings))
    return report


def render_report(report: PreflightReport) -> str:
    """Machine-greppable, secret-free rendering."""
    lines = ["BOOT PREFLIGHT", f"  result : {'OK' if report.ok else 'REFUSED'}"]
    for phase in report.phases:
        mark = "ok  " if phase.ok else "FAIL"
        lines.append(f"  [{mark}] {phase.name:10} {phase.detail}")
        for failure in phase.failures:
            for sub in failure.splitlines():
                lines.append(f"         {sub}")
    if not report.ok:
        lines.append(f"  exit_code : {report.exit_code}")
    return "\n".join(lines)


def write_report(report: PreflightReport, settings: AnitaSettings | None) -> Path | None:
    """Persist the preflight report under the isolated log directory.

    **Skipped when isolation failed.** Writing a report into the directory the
    guard just refused would be a side effect on the refusal path — the same
    class of mistake as binding a port or opening a connection after refusing.
    A refused boot must leave nothing behind, including its own diagnostics.

    (The refusal is still fully visible: the preflight prints to stdout, which
    is where an operator is already looking.)
    """
    if settings is None:
        return None
    if any(phase.name == "ISOLATION" and not phase.ok for phase in report.phases):
        return None
    log_dir = settings.log_dir_path
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "preflight.json"
    path.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m anita.boot",
        description="ANITA platform boot preflight and start orchestration.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--preflight",
        action="store_true",
        help="validate configuration only; starts nothing (exit 0 ok / 2 refused / 3 config error)",
    )
    group.add_argument("--start", action="store_true", help="run preflight then start the stack")
    parser.add_argument("--config", default=None, help="path to anita.yaml")
    parser.add_argument("--project-root", default=None)
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)

    report = preflight(config_path=args.config, project_root=args.project_root)

    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(render_report(report))

    written = write_report(report, report.settings)
    if written and not args.json:
        print(f"  report : {written}")
    elif not report.ok and not args.json:
        print("  report : not written (refused before any side effect)")

    if not report.ok:
        return report.exit_code

    if args.preflight:
        return EXIT_OK

    # --start: the service graph lands with MOD-01/02. Until then, a green
    # preflight is the deliverable, and saying so is more honest than pretending
    # to start something that does not exist yet.
    print("\n  preflight green — service start lands with MOD-01/MOD-02 "
          "(see docs/sdlc/stories/US-028/US-029/US-030)")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
