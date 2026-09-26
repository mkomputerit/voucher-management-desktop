"""Milestone C tests for shared ProgramData migration and safety boundaries."""

from __future__ import annotations

from pathlib import Path

from voucher_management.database import Database
from voucher_management.history import HistoryService
from voucher_management.paths import AppPaths
from voucher_management.security.history_key import HistoryKeyStore
from voucher_management.settings import DEFAULT_SETTINGS, SettingsStore
from voucher_management.shared_data_migration import (
    SharedDataMigrationError,
    execute_shared_data_migration,
    shared_target_is_pristine,
    source_has_migratable_data,
)


def _initialize_history(root: Path) -> None:
    settings_store = SettingsStore(root / "config" / "settings.json")
    HistoryService(
        root / "data" / "history.jsonl",
        root / "data" / "history.lock",
        settings_store,
        secret_store=HistoryKeyStore(root),
    )


def test_fresh_shared_target_is_pristine_after_bootstrap(tmp_path):
    root = tmp_path / "ProgramData" / "VoucherManagement"
    paths = AppPaths(
        base_override=tmp_path / "program",
        shared_root_override=root,
    )
    paths.ensure_writable()
    database = Database(paths.database)
    try:
        database.initialize()
        _initialize_history(root)

        assert shared_target_is_pristine(database, paths) is True
    finally:
        database.close()


def test_shared_target_stops_being_pristine_after_real_data(tmp_path):
    root = tmp_path / "ProgramData" / "VoucherManagement"
    paths = AppPaths(
        base_override=tmp_path / "program",
        shared_root_override=root,
    )
    paths.ensure_writable()
    database = Database(paths.database)
    try:
        database.initialize()
        _initialize_history(root)
        database.create_controller(
            name="Controller",
            api_root="https://controller.example/proxy/network/integration/v1",
            created_at="2026-09-26T10:00:00+00:00",
        )

        assert shared_target_is_pristine(database, paths) is False
    finally:
        database.close()


def test_future_schema_table_with_data_makes_target_non_pristine(tmp_path):
    root = tmp_path / "ProgramData" / "VoucherManagement"
    paths = AppPaths(
        base_override=tmp_path / "program",
        shared_root_override=root,
    )
    paths.ensure_writable()
    database = Database(paths.database)
    try:
        database.initialize()
        _initialize_history(root)
        database.connection.execute(
            "CREATE TABLE future_schema_data (id INTEGER PRIMARY KEY, value TEXT)"
        )
        database.connection.execute(
            "INSERT INTO future_schema_data(value) VALUES ('operational')"
        )
        database.connection.commit()

        assert shared_target_is_pristine(database, paths) is False
    finally:
        database.close()


def test_only_schema_metadata_is_allowed_in_pristine_target(tmp_path):
    root = tmp_path / "ProgramData" / "VoucherManagement"
    paths = AppPaths(
        base_override=tmp_path / "program",
        shared_root_override=root,
    )
    paths.ensure_writable()
    database = Database(paths.database)
    try:
        database.initialize()
        _initialize_history(root)
        database.connection.execute(
            "INSERT INTO app_metadata(key, value) VALUES ('future_marker', '1')"
        )
        database.connection.commit()

        assert shared_target_is_pristine(database, paths) is False
    finally:
        database.close()


def test_source_detection_ignores_empty_tree_but_finds_settings(tmp_path):
    root = tmp_path / "profile" / "VoucherManagement"
    (root / "data").mkdir(parents=True)

    assert source_has_migratable_data(root) is False

    store = SettingsStore(root / "config" / "settings.json")
    store.save(dict(DEFAULT_SETTINGS))

    assert source_has_migratable_data(root) is True


def test_per_user_tree_migrates_through_verified_encrypted_backup(tmp_path):
    source_root = tmp_path / "profile" / "VoucherManagement"
    source_root.mkdir(parents=True)
    _initialize_history(source_root)
    source_store = SettingsStore(source_root / "config" / "settings.json")
    source_settings = source_store.load()
    source_settings["wifi_title"] = "Sala ospiti"
    source_store.save(source_settings)

    source_db = Database(source_root / "data" / "voucher_management.db")
    source_db.initialize()
    source_db.create_controller(
        name="Legacy controller",
        api_root="https://legacy.example/proxy/network/integration/v1",
        created_at="2026-09-20T10:00:00+00:00",
    )
    source_db.close()

    target_root = tmp_path / "ProgramData" / "VoucherManagement"
    target_paths = AppPaths(
        base_override=tmp_path / "program",
        shared_root_override=target_root,
    )
    target_paths.ensure_writable()
    target_db = Database(target_paths.database)
    target_db.initialize()
    _initialize_history(target_root)
    assert shared_target_is_pristine(target_db, target_paths) is True
    target_db.close()

    backup = tmp_path / "safety" / "migration.vmbk"
    result = execute_shared_data_migration(
        source_root=source_root,
        target_paths=target_paths,
        backup_destination=backup,
        backup_password="correct horse battery",
    )

    assert result.backup_path == backup
    assert backup.is_file()
    assert result.rollback_path.is_dir()

    migrated = Database(target_paths.database)
    try:
        migrated.initialize()
        migrated.integrity_check()
        assert (
            migrated.connection.execute(
                "SELECT name FROM controllers"
            ).fetchone()[0]
            == "Legacy controller"
        )
    finally:
        migrated.close()

    restored_settings = SettingsStore(target_paths.settings).load()
    assert restored_settings["wifi_title"] == "Sala ospiti"
    assert not (
        target_paths.data / "application.instance.lock"
    ).exists()


def test_migration_refuses_backup_inside_shared_target(tmp_path):
    source_root = tmp_path / "profile" / "VoucherManagement"
    source_store = SettingsStore(source_root / "config" / "settings.json")
    source_store.save(dict(DEFAULT_SETTINGS))

    target_root = tmp_path / "ProgramData" / "VoucherManagement"
    target_paths = AppPaths(
        base_override=tmp_path / "program",
        shared_root_override=target_root,
    )
    target_paths.ensure_writable()

    try:
        execute_shared_data_migration(
            source_root=source_root,
            target_paths=target_paths,
            backup_destination=target_root / "unsafe.vmbk",
            backup_password="correct horse battery",
        )
    except SharedDataMigrationError as exc:
        assert "fuori dai dati applicativi" in str(exc)
    else:
        raise AssertionError("backup inside target root must be rejected")
