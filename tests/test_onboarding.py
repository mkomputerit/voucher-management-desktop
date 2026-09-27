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
    complete_onboarding,
    onboarding_state,
)
from voucher_management.onboarding_ui import schedule_first_run_onboarding
from voucher_management.settings import SettingsStore


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
    )


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
        assert retention["unused_unprinted_days"] == DEFAULT_VOUCHER_RETENTION_DAYS
        assert retention["protect_used"] == 1
        assert retention["protect_printed"] == 1

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


def test_scheduler_runs_wizard_only_for_required_first_run(tmp_path):
    database = _database(tmp_path)
    scheduled = []
    launched = []
    app = type(
        "FakeApp",
        (),
        {
            "database": database,
            "after_idle": lambda self, callback: scheduled.append(callback),
        },
    )()
    try:
        state = schedule_first_run_onboarding(
            app,
            wizard_factory=lambda current: launched.append(current),
        )
        assert state is OnboardingState.REQUIRED
        assert len(scheduled) == 1
        assert launched == []

        scheduled[0]()
        assert launched == [app]
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
            "after_idle": lambda self, callback: scheduled.append(callback),
        },
    )()
    try:
        state = schedule_first_run_onboarding(app)
        assert state is OnboardingState.EXISTING_INSTALLATION
        assert scheduled == []
    finally:
        database.close()
