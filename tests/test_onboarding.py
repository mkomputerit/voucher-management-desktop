"""Tests for first-run onboarding state and persistence."""

from __future__ import annotations

from dataclasses import fields

import pytest

from voucher_management.database import Database
from voucher_management.onboarding import (
    DEFAULT_VOUCHER_RETENTION_DAYS,
    OnboardingDraft,
    OnboardingState,
    begin_onboarding,
    choose_shared_fresh_start,
    complete_onboarding,
    legacy_installation_has_evidence,
    onboarding_state,
)
from voucher_management.modern_app import ModernVoucherApp
from voucher_management.onboarding_ui import (
    schedule_first_run_onboarding,
    startup_onboarding_state,
)
from voucher_management.retention import retention_intro_seen
from voucher_management.settings import DEFAULT_SETTINGS, SettingsStore


NOW = "2026-09-27T08:00:00+00:00"


def _database(tmp_path):
    database = Database(tmp_path / "voucher_management.db")
    database.initialize()
    return database


def _draft():
    return OnboardingDraft(
        installation_name="Postazione reception",
        description="PC condiviso operatori",
        structure_type="Sede",
        structure_name="Sala Assemblee",
        wifi_title="Wi-Fi ospiti",
        pdf_title="Accesso Wi-Fi",
        pdf_subtitle="Voucher temporaneo",
        pdf_contact="Reception",
        pdf_notes="Conservare il voucher",
        unused_unprinted_days=180,
    )


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (OnboardingState.EXISTING_INSTALLATION, True),
        (OnboardingState.COMPLETE, True),
        (OnboardingState.REQUIRED, False),
        (OnboardingState.MIGRATION_AVAILABLE, False),
    ],
)
def test_retention_startup_gate_covers_completed_upgrades(
    monkeypatch,
    state,
    expected,
):
    monkeypatch.setattr(
        "voucher_management.modern_app.startup_onboarding_state",
        lambda _app: state,
    )
    fake = object()
    assert ModernVoucherApp._retention_intro_allowed_on_startup(fake) is expected


def test_fresh_database_requires_onboarding(tmp_path):
    database = _database(tmp_path)
    try:
        assert onboarding_state(database) is OnboardingState.REQUIRED
    finally:
        database.close()


def test_interrupted_onboarding_stays_required_after_partial_controller_data(
    tmp_path,
):
    database = _database(tmp_path)
    try:
        begin_onboarding(database)
        database.create_controller(
            name="Partially persisted controller",
            api_root="https://controller.example/proxy/network/integration/v1",
            created_at=NOW,
        )

        assert onboarding_state(database) is OnboardingState.REQUIRED
    finally:
        database.close()


def test_backup_audit_history_is_not_treated_as_fresh_installation(tmp_path):
    database = _database(tmp_path)
    try:
        database.record_backup_history(
            started_at=NOW,
            completed_at=NOW,
            destination="MANUAL",
            filename="audit-only.vmbk",
            status="SUCCESS",
            sha256="a" * 64,
            backup_format=2,
            schema_version=2,
        )

        assert onboarding_state(database) is OnboardingState.EXISTING_INSTALLATION
    finally:
        database.close()


def test_existing_operational_database_is_not_forced_through_new_install(tmp_path):
    database = _database(tmp_path)
    try:
        database.create_controller(
            name="Existing controller",
            api_root="https://controller.example/proxy/network/integration/v1",
            created_at=NOW,
        )

        assert onboarding_state(database) is OnboardingState.EXISTING_INSTALLATION
    finally:
        database.close()


def test_complete_onboarding_persists_profile_retention_and_nonsecret_settings(
    tmp_path,
):
    database = _database(tmp_path)
    store = SettingsStore(tmp_path / "settings.json")
    try:
        settings = complete_onboarding(
            database,
            store,
            _draft(),
            observed_at=NOW,
        )

        assert onboarding_state(database) is OnboardingState.COMPLETE
        profile = database.installation_profile()
        assert profile["installation_name"] == "Postazione reception"
        assert profile["description"] == "PC condiviso operatori"
        assert profile["pdf_title"] == "Accesso Wi-Fi"
        assert profile["pdf_contact"] == "Reception"

        retention = database.retention_policy()
        assert retention["unused_unprinted_days"] == 180
        assert retention["protect_used"] == 1
        assert retention["protect_printed"] == 1
        assert retention_intro_seen(database) is True

        assert settings["structure_name"] == "Sala Assemblee"
        assert settings["wifi_title"] == "Wi-Fi ospiti"
        assert not any(
            token in settings
            for token in ("api_key", "password", "token", "secret")
        )
    finally:
        database.close()


def test_onboarding_completion_is_idempotent_and_updates_profile(tmp_path):
    database = _database(tmp_path)
    store = SettingsStore(tmp_path / "settings.json")
    try:
        complete_onboarding(database, store, _draft(), observed_at=NOW)
        updated = OnboardingDraft(
            **{
                **_draft().__dict__,
                "installation_name": "Postazione aggiornata",
                "unused_unprinted_days": 365,
            }
        )
        complete_onboarding(
            database,
            store,
            updated,
            observed_at="2026-09-27T09:00:00+00:00",
        )

        assert database.connection.execute(
            "SELECT COUNT(*) FROM installation_profile"
        ).fetchone()[0] == 1
        assert database.installation_profile()["installation_name"] == (
            "Postazione aggiornata"
        )
        assert database.retention_policy()["unused_unprinted_days"] == 365
    finally:
        database.close()


def test_sqlite_failure_never_writes_completion_marker(tmp_path, monkeypatch):
    database = _database(tmp_path)
    store = SettingsStore(tmp_path / "settings.json")
    try:
        original = database.upsert_installation_profile

        def fail_profile(**kwargs):
            raise RuntimeError("synthetic profile failure")

        monkeypatch.setattr(
            database,
            "upsert_installation_profile",
            fail_profile,
        )

        with pytest.raises(RuntimeError, match="synthetic profile failure"):
            complete_onboarding(
                database,
                store,
                _draft(),
                observed_at=NOW,
            )

        assert database.installation_profile() is None
        assert database.retention_policy() is None
        assert onboarding_state(database) is OnboardingState.REQUIRED

        monkeypatch.setattr(
            database,
            "upsert_installation_profile",
            original,
        )
    finally:
        database.close()


def test_onboarding_draft_has_no_credential_fields():
    names = {field.name.lower() for field in fields(OnboardingDraft)}

    forbidden = ("api_key", "password", "secret", "token", "credential")
    assert not any(
        forbidden_name in field_name
        for field_name in names
        for forbidden_name in forbidden
    )


@pytest.mark.parametrize("days", [0, -1, 3651])
def test_onboarding_rejects_unsafe_retention(days, tmp_path):
    database = _database(tmp_path)
    store = SettingsStore(tmp_path / "settings.json")
    try:
        draft = OnboardingDraft(
            **{
                **_draft().__dict__,
                "unused_unprinted_days": days,
            }
        )
        with pytest.raises(ValueError, match="retention"):
            complete_onboarding(
                database,
                store,
                draft,
                observed_at=NOW,
            )

        assert database.installation_profile() is None
    finally:
        database.close()


def _startup_app(tmp_path, database, *, settings=None, shared_mode=False):
    root = tmp_path / "active"
    paths = type(
        "Paths",
        (),
        {
            "shared_mode": shared_mode,
            "per_user_root": tmp_path / "profile",
            "history": root / "data" / "history.jsonl",
            "prints": root / "Print",
            "logos": root / "Loghi",
        },
    )()
    return type(
        "FakeApp",
        (),
        {
            "database": database,
            "paths": paths,
            "settings": dict(DEFAULT_SETTINGS if settings is None else settings),
            "after_idle": lambda self, callback: None,
        },
    )()


def test_portable_4x_settings_are_not_forced_through_new_install(tmp_path):
    database = _database(tmp_path)
    settings = dict(DEFAULT_SETTINGS)
    settings["controller_api_root"] = (
        "https://controller.example/proxy/network/integration/v1"
    )
    app = _startup_app(tmp_path, database, settings=settings)
    try:
        assert (
            startup_onboarding_state(app)
            is OnboardingState.EXISTING_INSTALLATION
        )
    finally:
        database.close()


def test_portable_4x_history_is_not_forced_through_new_install(tmp_path):
    database = _database(tmp_path)
    app = _startup_app(tmp_path, database)
    app.paths.history.parent.mkdir(parents=True)
    app.paths.history.write_text('{"type":"print"}\n', encoding="utf-8")
    try:
        assert (
            startup_onboarding_state(app)
            is OnboardingState.EXISTING_INSTALLATION
        )
    finally:
        database.close()


def test_history_fingerprint_bootstrap_alone_still_requires_onboarding(tmp_path):
    database = _database(tmp_path)
    settings = dict(DEFAULT_SETTINGS)
    settings["history_key_fingerprint"] = "bootstrap"
    app = _startup_app(tmp_path, database, settings=settings)
    try:
        assert startup_onboarding_state(app) is OnboardingState.REQUIRED
    finally:
        database.close()


def test_interrupted_onboarding_marker_wins_over_partial_filesystem_state(
    tmp_path,
):
    database = _database(tmp_path)
    settings = dict(DEFAULT_SETTINGS)
    settings["structure_name"] = "Parzialmente configurata"
    app = _startup_app(tmp_path, database, settings=settings)
    try:
        begin_onboarding(database)
        assert startup_onboarding_state(app) is OnboardingState.REQUIRED
    finally:
        database.close()


def test_legacy_evidence_helper_fails_closed_on_managed_files(tmp_path):
    root = tmp_path / "legacy"
    paths = type(
        "Paths",
        (),
        {
            "history": root / "data" / "history.jsonl",
            "prints": root / "Print",
            "logos": root / "Loghi",
        },
    )()
    paths.logos.mkdir(parents=True)
    (paths.logos / "logo.png").write_bytes(b"synthetic")

    assert legacy_installation_has_evidence(paths, dict(DEFAULT_SETTINGS)) is True


def test_scheduler_runs_wizard_only_for_required_first_run(tmp_path):
    database = _database(tmp_path)
    scheduled = []
    launched = []
    app = type(
        "FakeApp",
        (),
        {
            "database": database,
            "paths": type(
                "Paths",
                (),
                {
                    "shared_mode": False,
                    "per_user_root": tmp_path / "profile",
                },
            )(),
            "after": lambda self, delay, callback: scheduled.append(
                (delay, callback)
            ),
            "winfo_exists": lambda self: True,
            "deiconify": lambda self: None,
            "lift": lambda self: None,
        },
    )()
    try:
        state = schedule_first_run_onboarding(
            app,
            wizard_factory=lambda current: launched.append(current),
        )
        assert state is OnboardingState.REQUIRED
        assert len(scheduled) == 1
        assert scheduled[0][0] == 320
        assert launched == []

        scheduled[0][1]()
        assert launched == [app]
    finally:
        database.close()


def test_shared_first_run_defers_to_explicit_per_user_migration(tmp_path):
    database = _database(tmp_path)
    per_user = tmp_path / "LocalAppData" / "VoucherManagement"
    store = SettingsStore(per_user / "config" / "settings.json")
    store.save({"structure_name": "Legacy Sala"})

    scheduled = []
    app = type(
        "FakeApp",
        (),
        {
            "database": database,
            "paths": type(
                "Paths",
                (),
                {
                    "shared_mode": True,
                    "per_user_root": per_user,
                },
            )(),
            "after_idle": lambda self, callback: scheduled.append(callback),
        },
    )()
    try:
        state = schedule_first_run_onboarding(app)
        assert state is OnboardingState.MIGRATION_AVAILABLE
        assert scheduled == []
    finally:
        database.close()


def test_shared_first_run_can_explicitly_start_fresh_without_deleting_legacy(
    tmp_path,
):
    database = _database(tmp_path)
    per_user = tmp_path / "LocalAppData" / "VoucherManagement"
    store = SettingsStore(per_user / "config" / "settings.json")
    store.save({"structure_name": "Legacy Sala"})

    app = type(
        "FakeApp",
        (),
        {
            "database": database,
            "paths": type(
                "Paths",
                (),
                {
                    "shared_mode": True,
                    "per_user_root": per_user,
                },
            )(),
            "settings": dict(DEFAULT_SETTINGS),
            "after_idle": lambda self, callback: None,
        },
    )()
    try:
        assert (
            startup_onboarding_state(app)
            is OnboardingState.MIGRATION_AVAILABLE
        )

        choose_shared_fresh_start(database)

        assert startup_onboarding_state(app) is OnboardingState.REQUIRED
        assert (per_user / "config" / "settings.json").is_file()
        assert store.load()["structure_name"] == "Legacy Sala"
    finally:
        database.close()


def test_shared_fresh_start_does_not_override_interrupted_onboarding(tmp_path):
    database = _database(tmp_path)
    per_user = tmp_path / "profile"
    store = SettingsStore(per_user / "config" / "settings.json")
    store.save({"structure_name": "Legacy Sala"})
    app = _startup_app(tmp_path, database, shared_mode=True)
    app.paths.per_user_root = per_user
    try:
        choose_shared_fresh_start(database)
        begin_onboarding(database)

        assert startup_onboarding_state(app) is OnboardingState.REQUIRED
    finally:
        database.close()


def test_scheduler_does_not_force_existing_installation(tmp_path):
    database = _database(tmp_path)
    database.create_controller(
        name="Existing",
        api_root="https://controller.example/proxy/network/integration/v1",
        created_at=NOW,
    )
    scheduled = []
    app = type(
        "FakeApp",
        (),
        {
            "database": database,
            "paths": type(
                "Paths",
                (),
                {
                    "shared_mode": False,
                    "per_user_root": tmp_path / "profile",
                },
            )(),
            "after_idle": lambda self, callback: scheduled.append(callback),
        },
    )()
    try:
        state = schedule_first_run_onboarding(app)
        assert state is OnboardingState.EXISTING_INSTALLATION
        assert scheduled == []
    finally:
        database.close()



def test_onboarding_requires_explicit_retention_value(tmp_path):
    database = _database(tmp_path)
    store = SettingsStore(tmp_path / "settings.json")
    try:
        draft = OnboardingDraft(
            installation_name="Postazione reception",
            structure_name="Sala Assemblee",
            wifi_title="Wi-Fi ospiti",
        )
        with pytest.raises(ValueError):
            complete_onboarding(
                database,
                store,
                draft,
                observed_at=NOW,
            )
        assert database.installation_profile() is None
    finally:
        database.close()
