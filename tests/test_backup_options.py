"""Per-copy backup choices, wizard persistence and real Tk cancellation."""
from pathlib import Path
from types import SimpleNamespace
import sys
import tkinter as tk

import pytest

from voucher_management.backup_options_ui import (
    BackupChoice, BackupOptionsDialog, make_backup_choice, validate_backup_directory,
)
from voucher_management import data_maintenance_ui as maintenance
from voucher_management.data_maintenance_ui import DataMaintenanceMixin
from voucher_management.database import Database
from voucher_management.modern_app import ModernVoucherApp
from voucher_management.onboarding import OnboardingDraft, complete_onboarding
from voucher_management.settings import SettingsStore
from voucher_management.backup import BackupService


@pytest.fixture(scope="module")
def tk_root():
    try:
        root = tk.Tk()
    except tk.TclError:
        if sys.platform == "win32":
            raise
        pytest.skip("Tk display unavailable")
    yield root
    root.destroy()


@pytest.mark.parametrize("protected", [False, True])
def test_choice_uses_unambiguous_extension_and_never_passes_unused_secret(tmp_path, protected):
    choice = make_backup_choice(str(tmp_path), protected=protected, password="s" * 24,
                                confirmation="s" * 24)
    assert choice.target.parent == tmp_path
    assert choice.target.suffix == (".vmbk" if protected else ".zip")
    assert choice.password == ("s" * 24 if protected else None)


@pytest.mark.parametrize("password,confirmation", [("short", "short"), ("sample-passphrase", "different-value")])
def test_protected_copy_requires_valid_matching_passwords(tmp_path, password, confirmation):
    with pytest.raises(ValueError):
        make_backup_choice(str(tmp_path), protected=True, password=password, confirmation=confirmation)


def test_backup_destination_cannot_be_relative_or_inside_managed_tree(tmp_path):
    with pytest.raises(ValueError):
        validate_backup_directory("relative")
    with pytest.raises(ValueError):
        validate_backup_directory(str(tmp_path / "data" / "copies"), tmp_path)
    assert validate_backup_directory(str(tmp_path / "external"), tmp_path / "app") == tmp_path / "external"


@pytest.mark.parametrize("skip", [False, True])
def test_close_temporary_folder_does_not_change_default_and_password_can_be_omitted(tmp_path, monkeypatch, skip):
    settings = {"backup_directory": str(tmp_path / "default"), "backup_on_close": True}
    calls = []
    choice = BackupChoice(skip=True) if skip else BackupChoice(tmp_path / "other" / "copy.zip")
    def prompt(parent, **kwargs):
        assert kwargs["default_directory"] == settings["backup_directory"]
        assert kwargs["closing"]
        return choice
    monkeypatch.setattr(maintenance, "ask_backup_options", prompt)
    fake = SimpleNamespace(settings=settings.copy(), paths=SimpleNamespace(automatic_backups=tmp_path / "fallback"),
                           _background_results=None, _start_close_backup=lambda *args: calls.append(args),
                           _finish_close=lambda **kwargs: calls.append(kwargs))
    DataMaintenanceMixin.request_close(fake)
    assert fake.settings == settings
    if skip:
        assert calls == [{"close_status": "CLOSED_WITHOUT_BACKUP", "backup_status": "SKIPPED"}]
    else:
        assert calls == [(choice.target, None)]


def test_wizard_persists_backup_options_and_export_drops_machine_specific_folder(tmp_path):
    database = Database(tmp_path / "db.sqlite")
    database.initialize()
    store = SettingsStore(tmp_path / "settings.json")
    try:
        draft = OnboardingDraft(
            installation_name="Reception",
            structure_name="Example Venue",
            backup_directory=str(tmp_path / "copies"),
            backup_on_close=False,
            unused_unprinted_days=180,
        )
        complete_onboarding(database, store, draft, observed_at="2026-09-29T06:00:00+00:00")
        settings = store.load()
        assert settings["backup_directory"] == str(tmp_path / "copies")
        assert settings["backup_on_close"] is False
        import json
        exported = json.loads(BackupService._sanitized_settings_bytes(store.path))
        assert exported["backup_directory"] == ""
    finally:
        database.close()


def test_home_uses_only_successful_backup_history(tmp_path):
    database = Database(tmp_path / "db.sqlite")
    database.initialize()
    class Var:
        def set(self, value):
            self.value = value
    fake = SimpleNamespace(database=database, home_backup_summary_var=Var(), settings_backup_summary_var=Var())
    try:
        ModernVoucherApp._refresh_backup_summary(fake)
        assert "mai eseguito" in fake.home_backup_summary_var.value
        database.record_backup_history(started_at="2026-09-28T10:00:00+00:00", completed_at="2026-09-28T10:01:00+00:00",
            destination="MANUAL", filename="good.zip", status="SUCCESS", sha256="a"*64, backup_format=2, schema_version=1)
        database.record_backup_history(started_at="2026-09-29T10:00:00+00:00", completed_at="2026-09-29T10:01:00+00:00",
            destination="MANUAL", filename="failed.zip", status="FAILED", error_summary="OSError")
        ModernVoucherApp._refresh_backup_summary(fake)
        assert "28/09/2026" in fake.home_backup_summary_var.value
        assert "good.zip" in fake.settings_backup_summary_var.value
    finally:
        database.close()


def test_real_backup_dialog_defaults_to_protected_copy_and_plaintext_requires_opt_out(tmp_path, tk_root):
    root = tk_root
    dialog = BackupOptionsDialog(root, default_directory=str(tmp_path), closing=True)
    assert dialog.protected_var.get() is True
    dialog.password_var.set("synthetic-password")
    dialog.confirm_var.set("synthetic-password")
    dialog.accept()
    assert dialog.result.password == "synthetic-password"
    assert dialog.result.target.suffix == ".vmbk"

    dialog = BackupOptionsDialog(root, default_directory=str(tmp_path), closing=True)
    dialog.password_var.set("synthetic-password")
    dialog.confirm_var.set("synthetic-password")
    dialog.protected_var.set(False)
    dialog._toggle_password()
    assert dialog.password_var.get() == ""
    assert dialog.confirm_var.get() == ""
    dialog.accept()
    assert dialog.result.password is None
    assert dialog.result.target.suffix == ".zip"

    dialog = BackupOptionsDialog(root, default_directory=str(tmp_path), closing=True)
    dialog.skip()
    assert dialog.result.skip
    dialog = BackupOptionsDialog(root, default_directory=str(tmp_path), closing=True)
    dialog.destroy()
    assert dialog.result is None

def test_real_wizard_reaches_backup_step_and_saves_it_at_completion(tmp_path, monkeypatch, tk_root):
    from voucher_management import onboarding_ui
    from voucher_management.onboarding import onboarding_state, OnboardingState
    root = tk_root
    database = Database(tmp_path / "db.sqlite")
    database.initialize()
    root.database = database
    root.settings_store = SettingsStore(tmp_path / "settings.json")
    root.settings = root.settings_store.load()
    root.paths = SimpleNamespace(user_root=tmp_path / "app", automatic_backups=tmp_path / "fallback")
    root.logger = SimpleNamespace(error=lambda *args: None)
    root._finish_connection = lambda *args, **kwargs: None
    monkeypatch.setattr(onboarding_ui.messagebox, "showinfo", lambda *args, **kwargs: None)
    monkeypatch.setattr(onboarding_ui.messagebox, "showerror",
                        lambda *args, **kwargs: pytest.fail(f"Unexpected wizard error: {args}"))
    try:
        wizard = onboarding_ui.FirstRunWizard(root)
        wizard._next()
        assert wizard.page == wizard.PAGE_IDENTITY
        wizard.installation_name_var.set("Reception")
        wizard.structure_name_var.set("Example Venue")
        wizard._next()
        assert wizard.page == wizard.PAGE_CONTROLLER
        endpoint = "https://controller.example/proxy/network/integration/v1"
        wizard.controller_root_var.set(endpoint)
        wizard._verified_root = endpoint
        wizard._controller_result = SimpleNamespace(client=object(), info={}, vouchers=[], observed_at="2026-09-29T06:00:00+00:00")
        wizard._next()
        assert wizard.page == wizard.PAGE_RETENTION
        wizard.retention_days_var.set("180")
        wizard._next()
        assert wizard.page == wizard.PAGE_BACKUP
        wizard.backup_directory_var.set(str(tmp_path / "chosen"))
        wizard.backup_on_close_var.set(False)
        wizard._next()
        assert wizard.page == wizard.PAGE_SUMMARY
        wizard._next()
        assert not wizard.winfo_exists()
        assert root.settings_store.load()["backup_directory"] == str(tmp_path / "chosen")
        assert root.settings_store.load()["backup_on_close"] is False
        assert onboarding_state(database) is OnboardingState.COMPLETE
    finally:
        database.close()
