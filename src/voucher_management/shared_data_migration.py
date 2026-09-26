"""Explicit migration from one Windows user's legacy data root to ProgramData.

The installed 5.0 runtime must never silently merge per-user trees. This module
reuses the reviewed encrypted backup/restore format so the move to shared data
inherits WAL-safe snapshotting, archive validation and rollback semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .backup import BackupService
from .locking import LockTimeout, exclusive_file_lock
from .settings import DEFAULT_SETTINGS, SettingsStore
from .single_instance import InstanceAlreadyRunning, SingleInstanceGuard


class SharedDataMigrationError(RuntimeError):
    """Raised when a per-user root cannot be moved to shared ProgramData safely."""


@dataclass(frozen=True)
class DataRootPaths:
    """Minimal path surface required by BackupService for an arbitrary root."""

    user_root: Path

    def __post_init__(self) -> None:
        root = Path(self.user_root)
        object.__setattr__(self, "user_root", root)
        object.__setattr__(self, "config", root / "config")
        object.__setattr__(self, "data", root / "data")
        object.__setattr__(self, "logs", root / "logs")
        object.__setattr__(self, "logos", root / "Loghi")
        object.__setattr__(self, "prints", root / "Print")
        object.__setattr__(self, "history", root / "data" / "history.jsonl")
        object.__setattr__(self, "history_lock", root / "data" / "history.lock")
        object.__setattr__(
            self,
            "instance_lock",
            root / "application.instance.lock",
        )
        object.__setattr__(
            self,
            "legacy_instance_lock",
            root / "data" / "application.instance.lock",
        )
        object.__setattr__(
            self,
            "pending_create",
            root / "data" / "pending_create_guard",
        )
        object.__setattr__(
            self,
            "database",
            root / "data" / "voucher_management.db",
        )
        object.__setattr__(
            self,
            "settings",
            root / "config" / "settings.json",
        )


@dataclass(frozen=True)
class SharedDataMigrationResult:
    """Result of one verified per-user to ProgramData transfer."""

    backup_path: Path
    rollback_path: Path


def source_has_migratable_data(source_root: Path) -> bool:
    """Return whether the old per-user tree contains meaningful application data."""

    paths = DataRootPaths(Path(source_root))
    direct_files = (
        paths.settings,
        paths.history,
        paths.data / "history_secret.key",
        paths.database,
    )
    if any(path.is_file() and path.stat().st_size > 0 for path in direct_files):
        return True
    for folder in (paths.prints, paths.logos):
        if folder.is_dir() and any(item.is_file() for item in folder.rglob("*")):
            return True
    return False


def shared_target_is_pristine(database, paths) -> bool:
    """Allow migration only before the shared installation has real user data.

    A first shared startup creates an empty schema plus a new history identity.
    Those bootstrap artifacts are allowed. Controller/voucher/audit data,
    customized settings, PDFs or logos make replacement unsafe.
    """

    tables = (
        "controllers",
        "application_sessions",
        "installation_profile",
        "vouchers",
        "sync_runs",
        "voucher_events",
        "print_jobs",
        "migration_runs",
        "legacy_audit_events",
    )
    for table in tables:
        row = database.connection.execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()
        if int(row[0]) > 0:
            return False

    history = Path(paths.history)
    if history.is_file() and history.stat().st_size > 0:
        return False

    for folder in (Path(paths.prints), Path(paths.logos)):
        if folder.is_dir() and any(item.is_file() for item in folder.rglob("*")):
            return False

    settings = SettingsStore(Path(paths.settings)).load()
    for key, default in DEFAULT_SETTINGS.items():
        if key == "history_key_fingerprint":
            continue
        if settings.get(key) != default:
            return False
    return True


def _assert_destination_outside_root(destination: Path, root: Path) -> None:
    try:
        destination.resolve().relative_to(root.resolve())
    except ValueError:
        return
    raise SharedDataMigrationError(
        "Il backup di migrazione deve essere salvato fuori dai dati applicativi"
    )


def execute_shared_data_migration(
    *,
    source_root: Path,
    target_paths,
    backup_destination: Path,
    backup_password: str,
) -> SharedDataMigrationResult:
    """Move one quiescent per-user tree through an authenticated .vmbk backup.

    The caller must close the target SQLite connection before invoking this
    function. Source history is locked for the complete backup so even older
    4.x builds that predate the application-wide guard cannot append audit rows
    while the migration snapshot is being built.
    """

    source_paths = DataRootPaths(Path(source_root))
    target_root = Path(target_paths.user_root)
    destination = Path(backup_destination)

    if source_paths.user_root.resolve() == target_root.resolve():
        raise SharedDataMigrationError(
            "Origine e archivio condiviso coincidono"
        )
    if not source_has_migratable_data(source_paths.user_root):
        raise SharedDataMigrationError(
            "Non risultano dati utente precedenti da migrare"
        )
    if not str(backup_password or ""):
        raise SharedDataMigrationError(
            "La migrazione richiede un backup cifrato"
        )
    _assert_destination_outside_root(destination, source_paths.user_root)
    _assert_destination_outside_root(destination, target_root)

    # Acquire both the Milestone A lock location and the Milestone C location.
    # A currently running older portable build therefore blocks migration.
    guards = [
        SingleInstanceGuard(source_paths.legacy_instance_lock),
        SingleInstanceGuard(source_paths.instance_lock),
    ]
    acquired: list[SingleInstanceGuard] = []
    try:
        for guard in guards:
            guard.acquire()
            acquired.append(guard)
        try:
            with exclusive_file_lock(source_paths.history_lock, timeout=0.0):
                source_backup = BackupService(source_paths)
                backup_path = Path(
                    source_backup.create(
                        destination,
                        password=backup_password,
                    )
                )
                if (
                    not backup_path.is_file()
                    or not source_backup.is_encrypted_backup(backup_path)
                ):
                    raise SharedDataMigrationError(
                        "Backup pre-migrazione non verificabile"
                    )
                source_backup.validate_encrypted(
                    backup_path,
                    backup_password,
                )
        except LockTimeout as exc:
            raise SharedDataMigrationError(
                "La cronologia utente è in uso. Chiudere la versione precedente."
            ) from exc
    except InstanceAlreadyRunning as exc:
        raise SharedDataMigrationError(
            "La versione precedente di Voucher Management risulta ancora aperta."
        ) from exc
    except SharedDataMigrationError:
        raise
    except Exception as exc:
        raise SharedDataMigrationError(
            "Creazione/verifica del backup pre-migrazione non riuscita"
        ) from exc
    finally:
        for guard in reversed(acquired):
            guard.release()

    try:
        rollback = BackupService(target_paths).restore(
            backup_path,
            password=backup_password,
        )
    except Exception as exc:
        raise SharedDataMigrationError(
            "Ripristino dei dati nell'archivio condiviso non riuscito"
        ) from exc

    return SharedDataMigrationResult(
        backup_path=backup_path,
        rollback_path=Path(rollback),
    )
