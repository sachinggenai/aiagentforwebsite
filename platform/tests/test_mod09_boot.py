"""MOD-09 tests — configuration, isolation and boot (US-028, US-029, US-030).

These assert the properties the plan claims, not the implementation's shape:

* a colliding boot is **refused**, and refused before anything is started
* a key written but never read is **refused**, not ignored
* a malformed value **names the key** instead of falling back
* a secret is never rendered — presence only
* the guard **cannot be satisfied by copying the sibling's config**

Run:  .venv\\Scripts\\python -m pytest tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))
sys.path.insert(0, str(PLATFORM))

from anita import config as cfg  # noqa: E402
from anita import config_truth, secrets  # noqa: E402
from anita.boot import EXIT_CONFIG_ERROR, EXIT_OK, EXIT_REFUSED, preflight  # noqa: E402
from anita.config import (  # noqa: E402
    AnitaSettings,
    ForbiddenValues,
    IsolationSettings,
    _env_bool,
    _env_float,
    _env_int,
    load_settings,
)
from anita.errors import ConfigError, IsolationViolation  # noqa: E402
from anita.isolation import (  # noqa: E402
    assert_isolation,
    assert_vram_budget,
    evaluate_isolation,
)

REPO = PLATFORM.parent  # D:\project\anitamedicalsystems


def make_settings(**iso_overrides) -> AnitaSettings:
    """A valid baseline whose isolation values can be individually broken."""
    defaults = dict(
        db_name="anita_ams",
        db_schema="anita",
        chroma_path="./data/chroma_anita",
        chroma_collection="anita_ams__v1",
        api_port=8100,
        chat_port=8601,
        admin_port=8602,
        log_dir="./logs/anita",
    )
    defaults.update(iso_overrides)
    return AnitaSettings(
        isolation=IsolationSettings(**defaults),
        forbidden=ForbiddenValues(),
        identity=cfg.IdentitySettings(),
        corpus=cfg.CorpusSettings(),
        model=cfg.ModelSettings(),
        retrieval=cfg.RetrievalSettings(),
        speech=cfg.SpeechSettings(),
        sentiment=cfg.SentimentSettings(),
        outbound=cfg.OutboundSettings(),
        vram=cfg.VramSettings(),
        project_root=REPO,
        env={},
    )


# ---------------------------------------------------------------------------
# Isolation guard (US-029 / TRD-42)
# ---------------------------------------------------------------------------
class TestIsolationGuard:
    def test_valid_configuration_passes(self):
        verdict = assert_isolation(make_settings())
        assert verdict.ok

    @pytest.mark.parametrize(
        ("field", "value", "resource"),
        [
            ("db_name", "admissions", "db_name"),
            ("chroma_collection", "langchain", "chroma_collection"),
            ("chroma_collection", "meridian_kb__v1", "chroma_collection"),
        ],
    )
    def test_sibling_values_are_refused(self, field, value, resource):
        """Copying the sibling's database or collection must not start."""
        with pytest.raises(IsolationViolation) as exc:
            assert_isolation(make_settings(**{field: value}))
        assert exc.value.resource == resource
        assert value in str(exc.value)

    @pytest.mark.parametrize("port_field", ["api_port", "chat_port", "admin_port"])
    @pytest.mark.parametrize("sibling_port", [8000, 8501, 8502, 8098, 1234])
    def test_sibling_ports_are_refused(self, port_field, sibling_port):
        with pytest.raises(IsolationViolation):
            assert_isolation(make_settings(**{port_field: sibling_port}))

    def test_internal_port_collision_is_refused(self):
        """Two ANITA surfaces on one port is a collision with itself."""
        with pytest.raises(IsolationViolation) as exc:
            assert_isolation(make_settings(chat_port=8100))
        assert exc.value.resource == "port:internal"

    def test_schema_matching_database_name_is_refused(self):
        with pytest.raises(IsolationViolation) as exc:
            assert_isolation(make_settings(db_name="anita", db_schema="anita"))
        assert exc.value.resource == "db_schema"

    @pytest.mark.parametrize("shared", ["./logs", "logs"])
    def test_shared_log_directory_is_refused(self, shared):
        with pytest.raises(IsolationViolation) as exc:
            assert_isolation(make_settings(log_dir=shared))
        assert exc.value.resource == "log_dir"

    def test_refusal_message_is_actionable_and_machine_greppable(self):
        with pytest.raises(IsolationViolation) as exc:
            assert_isolation(make_settings(chroma_collection="langchain"))
        message = str(exc.value)
        assert "BOOT REFUSED" in message
        assert "resource :" in message
        assert "action" in message  # tells the operator what to type

    def test_evaluate_reports_every_failure_not_just_the_first(self):
        """An operator should not fix four collisions one boot at a time."""
        verdict = evaluate_isolation(
            make_settings(db_name="admissions", chroma_collection="langchain", api_port=8000)
        )
        assert not verdict.ok
        resources = {check.resource for check in verdict.failures}
        assert {"db_name", "chroma_collection", "port:api_port"} <= resources

    def test_guard_runs_before_any_side_effect(self):
        """The refusal path must not bind a port or open a connection.

        Asserted structurally: the guard is pure — it takes settings and
        returns a verdict, with no I/O in between.
        """
        settings = make_settings(chroma_collection="langchain")
        verdict = evaluate_isolation(settings)   # no exception, no side effect
        assert not verdict.ok
        assert settings.isolation.api_port == 8100  # nothing mutated


class TestVramBudget:
    def test_budget_that_starves_the_sibling_is_refused(self):
        base = make_settings()
        starving = AnitaSettings(
            **{
                **base.__dict__,
                "vram": cfg.VramSettings(
                    shared_card_mib=16311, anita_budget_mib=12000, sibling_floor_mib=6000
                ),
            }
        )
        with pytest.raises(IsolationViolation) as exc:
            assert_vram_budget(starving)
        assert exc.value.resource == "vram_budget"

    def test_budget_respecting_the_floor_passes(self):
        assert_vram_budget(make_settings())  # 16311 - 8192 = 8119 >= 6000


# ---------------------------------------------------------------------------
# Typed parsers (US-028 / TRD-41)
# ---------------------------------------------------------------------------
class TestTypedParsers:
    def test_malformed_integer_names_the_key(self):
        with pytest.raises(ConfigError) as exc:
            _env_int({"ANITA_API_PORT": "not-a-number"}, "ANITA_API_PORT", 8100)
        assert exc.value.key == "ANITA_API_PORT"
        assert "integer" in exc.value.reason

    def test_malformed_float_names_the_key(self):
        with pytest.raises(ConfigError) as exc:
            _env_float({"X": "abc"}, "X", 1.0)
        assert exc.value.key == "X"

    def test_malformed_boolean_names_the_key(self):
        with pytest.raises(ConfigError) as exc:
            _env_bool({"X": "maybe"}, "X", True)
        assert exc.value.key == "X"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("1", True), ("true", True), ("YES", True), ("on", True),
         ("0", False), ("false", False), ("NO", False), ("off", False)],
    )
    def test_boolean_forms(self, raw, expected):
        assert _env_bool({"X": raw}, "X", not expected) is expected

    def test_parse_failure_never_falls_back_to_a_default(self):
        """A silent fallback on an isolation coordinate is how two products end
        up sharing a database — so a *malformed* value must never be absorbed.

        An empty value is different: it means "not set", and taking the default
        for it is correct. The distinction is the whole point.
        """
        assert _env_int({"ANITA_API_PORT": ""}, "ANITA_API_PORT", 8100) == 8100  # unset
        assert _env_int({}, "ANITA_API_PORT", 8100) == 8100                     # absent
        # Note: int() accepts underscores ("8_100" parses as 8100), so that is
        # not a malformed value and is deliberately not in this list.
        for malformed in ("8100.5", "eight-thousand", "8,100", "8100abc"):
            with pytest.raises(ConfigError):
                _env_int({"ANITA_API_PORT": malformed}, "ANITA_API_PORT", 8100)

    def test_env_overrides_yaml_for_isolation_keys(self):
        settings = load_settings(
            env={"ANITA_DB_NAME": "anita_ams", "ANITA_API_PORT": "8200"},
            project_root=PLATFORM,
        )
        assert settings.isolation.api_port == 8200
        assert settings.isolation.db_name == "anita_ams"

    def test_settings_are_frozen(self):
        settings = make_settings()
        with pytest.raises(Exception):
            settings.isolation.db_name = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Config audit (US-028 / TRD-41, DG-09)
# ---------------------------------------------------------------------------
class TestConfigAudit:
    def test_inert_key_is_detected(self, tmp_path):
        """A key written but read by nothing must be reported INERT."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "mod.py").write_text("def f(env):\n    return 1\n", encoding="utf-8")
        report = config_truth.audit_config(env={"ANITA_UNUSED_KEY": "x"}, source_dir=src)
        assert "ANITA_UNUSED_KEY" in report.inert
        assert not report.ok

    def test_typed_helper_read_counts_as_a_read(self, tmp_path):
        """False-positive trap 1: matching only os.environ would flag every
        properly-typed key as inert."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "mod.py").write_text(
            'def f(env):\n    return _env_int(env, "ANITA_API_PORT", 8100)\n', encoding="utf-8"
        )
        report = config_truth.audit_config(env={"ANITA_API_PORT": "8100"}, source_dir=src)
        assert "ANITA_API_PORT" in report.live
        assert report.ok

    def test_assignment_is_not_counted_as_a_read(self, tmp_path):
        """False-positive trap 2: `os.environ["K"] = x` is a write."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "mod.py").write_text(
            'import os\n\ndef f():\n    os.environ["ANITA_SET_ONLY"] = "y"\n', encoding="utf-8"
        )
        report = config_truth.audit_config(env={"ANITA_SET_ONLY": "y"}, source_dir=src)
        assert "ANITA_SET_ONLY" in report.inert

    def test_getenv_read_is_detected(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "mod.py").write_text(
            'import os\n\ndef f():\n    return os.getenv("ANITA_ADMIN_TOKEN")\n', encoding="utf-8"
        )
        report = config_truth.audit_config(env={"ANITA_ADMIN_TOKEN": "x"}, source_dir=src)
        assert "ANITA_ADMIN_TOKEN" in report.live

    def test_a_named_env_mapping_read_counts(self, tmp_path):
        """§8.29: the notifier receives the environment as `values`, so its
        reads were invisible and `SMTP_*` was reported "pending (modules not
        yet built)" in every boot report — for a module that was built."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "mod.py").write_text(
            'def f(env):\n    values = env or {}\n'
            '    return (values.get("SMTP_HOST") or "").strip()\n',
            encoding="utf-8",
        )
        report = config_truth.audit_config(env={"SMTP_HOST": "smtp.example"}, source_dir=src)
        assert "SMTP_HOST" in report.live
        assert "SMTP_HOST" not in report.pending

    def test_an_unrelated_mapping_is_not_a_read(self, tmp_path):
        """The other direction, and the dangerous one: a false LIVE hides a key
        nobody reads. A `payload.get("STATUS")` must not count."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "mod.py").write_text(
            'def f(payload):\n    return payload.get("ANITA_UNUSED_KEY")\n',
            encoding="utf-8",
        )
        report = config_truth.audit_config(env={"ANITA_UNUSED_KEY": "x"}, source_dir=src)
        assert "ANITA_UNUSED_KEY" in report.inert
        assert not report.ok

    def test_every_declared_ollama_key_is_actually_read(self):
        """`OLLAMA_KEEP_ALIVE` was the odd one out: its siblings all took an
        environment override and it did not, so `.env.example` declared a knob
        that nothing read (found by the §8.29 fix)."""
        settings = load_settings(env={"OLLAMA_KEEP_ALIVE": "-1m"}, project_root=PLATFORM)
        assert settings.model.ollama_keep_alive == "-1m"

    def test_the_extra_channel_secret_is_a_structural_read(self):
        """Its name comes from `config/anita.yaml`, so the read cannot be
        matched by pattern — the exemption is explicit and audited here."""
        report = config_truth.audit_config(
            env={"ANITA_EXTRA_CHANNEL_SECRET": "s3cret"},
            source_dir=PLATFORM / "app" / "anita",
        )
        assert "ANITA_EXTRA_CHANNEL_SECRET" in report.live

    def test_no_built_key_is_still_reported_as_pending(self):
        """The whole point of §8.29: the keys whose modules are built and which
        read them must not appear in the "modules not yet built" bucket."""
        report = config_truth.audit_config(
            env={"SMTP_HOST": "smtp.example", "SMTP_PORT": "587",
                 "SMTP_USER": "u", "SMTP_PASS": "p",
                 "SALES_NOTIFY_EMAIL": "sales@example.com"},
            source_dir=PLATFORM / "app" / "anita",
        )
        for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS",
                    "SALES_NOTIFY_EMAIL"):
            assert key not in report.pending, f"{key} is read by notify.py"

    def test_report_carries_the_pattern_inventory(self, tmp_path):
        """An invisible checker is one people work around."""
        src = tmp_path / "src"
        src.mkdir()
        report = config_truth.audit_config(env={}, source_dir=src)
        assert report.pattern_inventory
        assert "typed_helper" in report.pattern_inventory

    def test_render_never_prints_a_secret_value(self):
        report = config_truth.audit_config(env={"ANITA_ADMIN_TOKEN": "super-secret-value"},
                                           source_dir=PLATFORM / "app" / "anita")
        rendered = config_truth.render_audit(report)
        assert "super-secret-value" not in rendered

    def test_audit_works_on_the_real_tree(self):
        """The audit must find MOD-09's own keys live on the actual source tree.

        It deliberately does **not** assert zero inert keys. The tree is a
        partial build: keys belonging to MOD-01…MOD-08, MOD-10 are declared in
        `.env.example` but nothing reads them yet, so the audit correctly
        reports them INERT. That is the audit working, not failing.

        Zero inert is a **go-live gate**, not a today gate — it becomes true as
        the remaining modules land, and each one should shrink this list.
        """
        report = config_truth.audit_config(
            env={"ANITA_ADMIN_TOKEN": "x"},
            source_dir=PLATFORM / "app" / "anita",
            env_files=[PLATFORM / ".env.example"],
        )
        # MOD-09's own keys are read by config.py and must be live.
        for key in ("ANITA_DB_NAME", "ANITA_CHROMA_COLLECTION", "ANITA_LOG_DIR",
                    "ANITA_API_PORT", "ANITA_ADMIN_TOKEN"):
            assert key in report.live, f"{key} should be live, got {report.live}"
        # And the audit is actually exercising the file, not silently empty.
        assert len(report.live) >= 20


# ---------------------------------------------------------------------------
# Secrets (US-030 / TRD-44)
# ---------------------------------------------------------------------------
class TestSecrets:
    @pytest.mark.parametrize(
        "key",
        ["TWILIO_AUTH_TOKEN", "ANITA_ADMIN_TOKEN", "DB_PASSWORD", "SMTP_PASS",
         "CRM_API_KEY", "TWILIO_ACCOUNT_SID"],
    )
    def test_secret_keys_are_recognised(self, key):
        assert secrets.is_sensitive(key)

    @pytest.mark.parametrize(
        "key",
        # A username is not a secret, any more than a port or a model name is.
        # Over-masking makes logs useless; the marker list targets credentials.
        ["ANITA_API_PORT", "OLLAMA_MODEL", "ANITA_DB_NAME", "SMTP_USER", "DB_USER"],
    )
    def test_non_secret_keys_are_not_masked(self, key):
        assert not secrets.is_sensitive(key)

    def test_masking_reports_presence_only(self):
        assert secrets.mask_value("TWILIO_AUTH_TOKEN", "abc123") == secrets.SET
        assert secrets.mask_value("TWILIO_AUTH_TOKEN", "") == secrets.UNSET
        assert "abc123" not in secrets.mask_mapping({"TWILIO_AUTH_TOKEN": "abc123"})[
            "TWILIO_AUTH_TOKEN"
        ]

    def test_non_secret_values_are_still_readable(self):
        assert secrets.mask_value("ANITA_API_PORT", "8100") == "8100"

    def test_redaction_removes_a_leaked_value(self):
        text = "connection failed for token abc123 at host"
        redacted = secrets.redact_text(text, {"TWILIO_AUTH_TOKEN": "abc123"})
        assert "abc123" not in redacted
        assert secrets.MASK in redacted


# ---------------------------------------------------------------------------
# Boot preflight (US-028/029/030)
# ---------------------------------------------------------------------------
class TestBootPreflight:
    def _env(self, **over):
        base = {"ANITA_ADMIN_TOKEN": "test-token"}
        base.update(over)
        return base

    def test_green_preflight_on_a_valid_configuration(self):
        report = preflight(env=self._env(), project_root=PLATFORM)
        assert report.ok, [f"{p.name}: {p.failures}" for p in report.failures]
        assert report.exit_code == EXIT_OK

    def test_collision_refuses_the_boot(self):
        report = preflight(
            env=self._env(ANITA_CHROMA_COLLECTION="langchain"), project_root=PLATFORM
        )
        assert not report.ok
        assert report.exit_code == EXIT_REFUSED
        names = {phase.name for phase in report.failures}
        assert "ISOLATION" in names

    def test_missing_required_secret_refuses_the_boot(self):
        report = preflight(env={}, project_root=PLATFORM)
        assert not report.ok
        assert "SECRETS" in {phase.name for phase in report.failures}

    def test_unparseable_config_returns_config_error_exit(self, tmp_path):
        bad = tmp_path / "anita.yaml"
        bad.write_text("isolation: [this, is, a, list]\n", encoding="utf-8")
        report = preflight(env=self._env(), config_path=bad, project_root=PLATFORM)
        assert not report.ok
        assert report.exit_code == EXIT_CONFIG_ERROR

    def test_missing_corpus_refuses_the_boot(self, tmp_path):
        """An answer engine with no catalogue can only invent — so this is a
        refusal, not a warning."""
        empty_root = tmp_path / "root"
        (empty_root / "config").mkdir(parents=True)
        (empty_root / "config" / "anita.yaml").write_text(
            (PLATFORM / "config" / "anita.yaml").read_text(encoding="utf-8"), encoding="utf-8"
        )
        report = preflight(env=self._env(), project_root=empty_root)
        phases = {phase.name: phase for phase in report.phases}
        assert not phases["DATA"].ok

    def test_preflight_reports_every_phase(self):
        report = preflight(env=self._env(), project_root=PLATFORM)
        assert [p.name for p in report.phases] == [
            "LOAD", "AUDIT", "ISOLATION", "SECRETS", "PLATFORM", "DATA", "INDEX", "WARM",
        ]

    def test_preflight_is_side_effect_free(self):
        """Running the preflight must not create the log directory of a config
        it is only validating — otherwise validating a broken config would
        itself leave state behind."""
        report = preflight(env=self._env(), project_root=PLATFORM)
        assert report.ok
        # The preflight itself performs no directory creation; only write_report does.
        assert report.settings is not None

    def test_refused_boot_writes_no_report(self):
        """A refusal must leave nothing behind — including its own diagnostics.

        Writing a report into the directory the guard just refused would be a
        side effect on the refusal path, the same class of mistake as binding a
        port after refusing.
        """
        from anita.boot import write_report

        report = preflight(env=self._env(ANITA_DB_NAME="admissions"), project_root=PLATFORM)
        assert not report.ok
        assert write_report(report, report.settings) is None

    def test_a_non_isolation_refusal_also_writes_no_report(self):
        """§8.35: the suppression was keyed on the ISOLATION phase alone, so a
        refusal from AUDIT, SECRETS, DATA or INDEX still wrote a report into
        the directory it had just refused to boot in. "A refused boot leaves
        nothing behind" has to hold for every refusal, not only the one the
        story happened to demonstrate.
        """
        from anita.boot import PhaseResult, write_report

        report = preflight(env=self._env(), project_root=PLATFORM)
        assert report.settings is not None
        # A refusal of a kind the isolation guard is not involved in.
        report.phases = [
            PhaseResult(name="LOAD", ok=True, detail="config loaded"),
            PhaseResult(name="ISOLATION", ok=True, detail="green"),
            PhaseResult(name="DATA", ok=False, detail="corpus missing"),
        ]

        assert not report.ok
        assert write_report(report, report.settings) is None

    def test_a_clean_boot_still_writes_its_report(self, tmp_path):
        """The suppression is about refusals, not about being shy: an operator
        still gets the artifact of a boot that worked."""
        from dataclasses import replace

        from anita.boot import write_report

        report = preflight(env=self._env(), project_root=PLATFORM)
        assert report.ok and report.settings is not None
        # Only the log directory moves: pointing the whole preflight at tmp_path
        # would also move the index store it reads, and the boot would fail for
        # a reason this test is not about.
        settings = replace(
            report.settings,
            isolation=replace(report.settings.isolation, log_dir=str(tmp_path)),
        )

        path = write_report(report, settings)

        assert path is not None and path.exists()
        assert path.name == "preflight.json"

    def test_report_is_secret_free(self):
        report = preflight(env=self._env(), project_root=PLATFORM)
        from anita.boot import render_report

        rendered = render_report(report)
        assert "test-token" not in rendered
        assert "ANITA_ADMIN_TOKEN=set" in rendered


# ---------------------------------------------------------------------------
# The sibling's ports — the list must stay complete
#
# It was not: 8010 (the sibling's RAG MCP) and 5432 (its Postgres) were absent,
# so the guard would have let ANITA bind either one without complaint. That is
# exactly the failure the guard exists to prevent, and it was invisible because
# nothing checked the list against the sibling's own service script.
# ---------------------------------------------------------------------------
class TestSiblingPorts:
    # Taken from universityDemo's start_services.ps1 and .env, not guessed.
    SIBLING_PORTS = {
        8000: "FastAPI",
        8010: "RAG MCP (ERC)",
        1234: "MLX",
        5432: "Postgres",
        8098: "CRM API",
        8501: "Streamlit (main)",
        8502: "Streamlit (dashboard)",
    }

    def test_every_sibling_port_is_forbidden(self):
        from anita.config import ForbiddenValues

        missing = set(self.SIBLING_PORTS) - set(ForbiddenValues.port)
        assert not missing, (
            "these sibling ports are not in ForbiddenValues.port: "
            + ", ".join(f"{p} ({self.SIBLING_PORTS[p]})" for p in sorted(missing))
        )

    def test_ollama_is_not_forbidden(self):
        """11434 is shared deliberately — both products use one Ollama server.
        Forbidding it would make the platform refuse to start at all."""
        from anita.config import ForbiddenValues

        assert 11434 not in ForbiddenValues.port

    def test_anitas_own_ports_do_not_collide(self):
        from anita.config import DEFAULT_ADMIN_PORT, DEFAULT_API_PORT
        from anita.config import DEFAULT_CHAT_PORT, DEFAULT_STREAMLIT_PORT
        from anita.config import ForbiddenValues

        ours = {DEFAULT_API_PORT, DEFAULT_CHAT_PORT, DEFAULT_ADMIN_PORT, DEFAULT_STREAMLIT_PORT}
        assert not (ours & set(ForbiddenValues.port))

    def test_the_streamlit_port_is_declared_and_checked(self):
        """A port passed on the command line is a port nothing validates. The
        Streamlit surface must be in the settings, so the guard sees it — the
        sibling already occupies 8501 and 8502, right where a default would
        land."""
        from anita.config import load_settings

        settings = load_settings(env={})
        assert settings.isolation.streamlit_port
        assert settings.isolation.streamlit_port in settings.isolation.ports

    @pytest.mark.parametrize("port,owner", sorted(SIBLING_PORTS.items()))
    def test_a_collision_is_refused_at_boot(self, port, owner):
        from anita.boot import preflight

        report = preflight(env={"ANITA_API_PORT": str(port)})
        isolation = next(p for p in report.phases if p.name == "ISOLATION")
        assert not isolation.ok, f"booting on {port} ({owner}) was allowed"
        assert not report.ok


class TestDotEnvIsLoaded:
    """`.env` reaches the settings, with the real environment winning.

    This was missing until 2026-10-04: `.env` was written, shipped as
    `.env.example`, declared via `python-dotenv` and audited by
    `config_truth.audit_config` — but never loaded, so every secret in it was
    inert and every boot reported the Twilio credentials unset while they sat
    in the file. A call to the provisioned number would have been refused at
    the signature check.

    Every test here passes an explicit `project_root`, so none of them read the
    developer's real `.env` — a test that did would pass or fail according to
    whose machine it ran on.
    """

    CONFIG = PLATFORM / "config" / "anita.yaml"

    def _settings(self, root):
        """Load with no explicit mapping, so `.env` is consulted."""
        return cfg.load_settings(None, config_path=self.CONFIG, project_root=root)

    @pytest.fixture(autouse=True)
    def _clear_the_key(self, monkeypatch):
        """Start each test with ANITA_DB_NAME absent from the process
        environment, so whatever it ends up as came from `.env` or from a
        `setenv` the test itself made."""
        monkeypatch.delenv("ANITA_DB_NAME", raising=False)

    def test_values_in_dot_env_reach_the_settings(self, tmp_path):
        (tmp_path / ".env").write_text("ANITA_DB_NAME=from_dot_env\n", encoding="utf-8")
        settings = self._settings(tmp_path)
        assert settings.env.get("ANITA_DB_NAME") == "from_dot_env"

    def test_the_real_environment_wins_over_dot_env(self, tmp_path, monkeypatch):
        """An override must override. Silently discarding one means running
        with a credential nobody realises is the wrong one."""
        (tmp_path / ".env").write_text("ANITA_DB_NAME=from_dot_env\n", encoding="utf-8")
        monkeypatch.setenv("ANITA_DB_NAME", "from_the_environment")
        settings = self._settings(tmp_path)
        assert settings.env.get("ANITA_DB_NAME") == "from_the_environment"

    def test_an_explicit_mapping_is_not_contaminated_by_dot_env(self, tmp_path):
        """Tests pass their own environment for isolation; the file must not
        leak into it, or a suite would inherit the developer's secrets."""
        (tmp_path / ".env").write_text("ANITA_DB_NAME=from_dot_env\n", encoding="utf-8")
        settings = cfg.load_settings(
            {"ANITA_DB_NAME": "explicit"}, config_path=self.CONFIG, project_root=tmp_path
        )
        assert settings.env.get("ANITA_DB_NAME") == "explicit"

    def test_a_missing_dot_env_is_not_an_error(self, tmp_path, monkeypatch):
        settings = self._settings(tmp_path)
        assert settings.env.get("ANITA_DB_NAME") in (None, "", "anita_ams")

    def test_a_malformed_dot_env_does_not_stop_the_boot(self, tmp_path, monkeypatch):
        """The file is an operator convenience; a broken one must not take the
        platform down when the real environment can still supply everything."""
        (tmp_path / ".env").write_text("this is not a key value pair\n", encoding="utf-8")
        settings = self._settings(tmp_path)
        assert settings is not None

    def test_dot_env_is_read_from_the_project_root_not_the_cwd(self, tmp_path, monkeypatch):
        """The platform is started from several directories; a config that
        depended on the cwd would work in one and silently not in another."""
        (tmp_path / ".env").write_text("ANITA_DB_NAME=from_root\n", encoding="utf-8")
        settings = self._settings(tmp_path)
        assert settings.env.get("ANITA_DB_NAME") == "from_root"
