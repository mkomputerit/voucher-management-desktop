"""Portable application-data backup and restore.

Backups contain only application-managed state. Archive validation is performed
before extraction because restored data can influence lifecycle decisions such
as whether an issued voucher may still be deleted.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .backup_crypto import (
    MIN_PASSWORD_CHARS,
    ProtectedBackupAuthenticationError,
    ProtectedBackupError,
    ProtectedBackupWriter,
    decrypt_backup_file,
    decrypt_backup_to_file,
    is_protected_backup,
    validate_backup_password,
)
from .identity import PRODUCT_DIR_NAME, PRODUCT_NAME
from .logo_validation import (
    LogoLimitError,
    LogoValidationError,
    validate_logo_image,
)
from .security.history_key import HistoryKeyStore
from .settings import SettingsStore


BACKUP_FORMAT = 2
SUPPORTED_BACKUP_FORMATS = {1, BACKUP_FORMAT}
MAX_ARCHIVE_FILES = 10_000
MAX_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
SQLITE_MEMBER = "data/voucher_management.db"
SQLITE_SIDECAR_MEMBERS = {
    "data/voucher_management.db-wal",
    "data/voucher_management.db-shm",
    "data/voucher_management.db-journal",
}

WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class BackupError(RuntimeError):
    """Raised when a backup cannot be created or safely restored."""


class BackupService:
    """Create and restore self-contained snapshots of application-managed data.

    The portable history key lives inside the data directory and is therefore
    included with history.jsonl. Format 1 backups remain readable so field-test
    data created by earlier betas is not stranded by the cleanup.
    """

    MANIFEST = "backup_manifest.json"
    DATA_DIRS = ("config", "data", "Print", "Loghi")
    ALLOWED_ROOTS = {MANIFEST, *DATA_DIRS, "security"}

    def __init__(self, paths):
        self.paths = paths
        self._restore_warnings: list[str] = []

    def _database_path(self) -> Path:
        """Return the live 5.0 SQLite path, including lightweight test paths."""

        return Path(
            getattr(
                self.paths,
                "database",
                self.paths.data / "voucher_management.db",
            )
        )

    @staticmethod
    def _sqlite_integrity_from_bytes(payload: bytes) -> int:
        """Verify one standalone SQLite image and return PRAGMA user_version."""

        if not payload:
            raise BackupError("Backup SQLite vuoto")
        connection = sqlite3.connect(":memory:")
        try:
            connection.deserialize(payload)
            rows = connection.execute("PRAGMA integrity_check").fetchall()
            if rows != [("ok",)]:
                raise BackupError("Backup SQLite non integro")
            return int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
        except (sqlite3.DatabaseError, sqlite3.OperationalError) as exc:
            raise BackupError(
                "Backup SQLite danneggiato o non leggibile"
            ) from exc
        finally:
            connection.close()

    def _sqlite_snapshot_bytes(self) -> tuple[bytes, int, str] | None:
        """Create a transactionally consistent snapshot with SQLite backup().

        The source may be in WAL mode. Opening a separate read connection lets
        SQLite include committed WAL pages in the online backup without copying
        the live .db/-wal/-shm files directly.
        """

        source = self._database_path()
        if not source.is_file():
            return None

        source_connection = None
        snapshot_connection = sqlite3.connect(":memory:")
        try:
            source_connection = sqlite3.connect(
                source,
                timeout=5.0,
            )
            source_connection.backup(snapshot_connection)
            # backup() copies page 1 verbatim, including WAL read/write flags
            # from the source. VACUUM rebuilds only the in-memory snapshot under
            # its own rollback-journal mode, producing a standalone image that
            # never requires a -wal sidecar after archive/restore.
            snapshot_connection.execute("VACUUM")
            rows = snapshot_connection.execute(
                "PRAGMA integrity_check"
            ).fetchall()
            if rows != [("ok",)]:
                raise BackupError("Snapshot SQLite non integro")
            user_version = int(
                snapshot_connection.execute(
                    "PRAGMA user_version"
                ).fetchone()[0]
            )
            payload = snapshot_connection.serialize()
            digest = hashlib.sha256(payload).hexdigest()
            return payload, user_version, digest
        except BackupError:
            raise
        except (sqlite3.DatabaseError, sqlite3.OperationalError, OSError) as exc:
            raise BackupError(
                "Impossibile creare uno snapshot SQLite consistente"
            ) from exc
        finally:
            if source_connection is not None:
                source_connection.close()
            snapshot_connection.close()

    @staticmethod
    def _copytree_without_live_sqlite(source: Path, destination: Path) -> None:
        """Copy application data while excluding live SQLite/WAL artifacts."""

        database_names = {
            "voucher_management.db",
            "voucher_management.db-wal",
            "voucher_management.db-shm",
            "voucher_management.db-journal",
        }

        def ignore(path, names):
            current = Path(path)
            if current.name == "data":
                return [name for name in names if name in database_names]
            return []

        shutil.copytree(source, destination, ignore=ignore)

    def consume_restore_warnings(self) -> tuple[str, ...]:
        """Return and clear non-fatal compatibility warnings from restore."""

        warnings = tuple(self._restore_warnings)
        self._restore_warnings.clear()
        return warnings

    def _assert_no_pending_print_audit(self) -> None:
        pending = self.paths.data / "pending_print_audit.json"
        if pending.exists():
            raise BackupError(
                "Esiste una stampa fisica ancora da registrare nello storico. "
                "Recuperare la stampa pendente prima di creare o ripristinare "
                "un backup."
            )

    def _assert_no_pending_create(self) -> None:
        pending = getattr(
            self.paths,
            "pending_create",
            self.paths.data / "pending_create_guard",
        )
        if Path(pending).exists():
            raise BackupError(
                "Esiste una creazione voucher con esito ancora da verificare. "
                "Sincronizzare l'elenco prima di creare o ripristinare un backup."
            )

    @staticmethod
    def is_encrypted_backup(source: Path) -> bool:
        """Return True for a Voucher Management protected backup container."""

        return is_protected_backup(Path(source))

    @staticmethod
    def _validated_backup_password(password: str) -> str:
        try:
            return validate_backup_password(password)
        except ValueError as exc:
            raise BackupError(str(exc)) from exc

    @staticmethod
    def _decrypt_to_zip(
        source: Path,
        destination_zip: Path,
        password: str,
    ) -> None:
        """Decrypt/authenticate through the single backup-crypto implementation."""

        try:
            decrypt_backup_file(
                Path(source),
                Path(destination_zip),
                password,
            )
        except ProtectedBackupAuthenticationError as exc:
            raise BackupError(
                "Password non valida oppure backup cifrato alterato"
            ) from exc
        except (ProtectedBackupError, ValueError) as exc:
            raise BackupError(str(exc)) from exc

    @staticmethod
    def _decrypt_to_stream(
        source: Path,
        target,
        password: str,
    ) -> None:
        """Decrypt/authenticate into an anonymous seekable temporary file."""

        try:
            decrypt_backup_to_file(
                Path(source),
                target,
                password,
            )
        except ProtectedBackupAuthenticationError as exc:
            raise BackupError(
                "Password non valida oppure backup cifrato alterato"
            ) from exc
        except (ProtectedBackupError, ValueError) as exc:
            raise BackupError(str(exc)) from exc

    @staticmethod
    def _zip_source(source):
        """Reset file-like ZIP sources while leaving path inputs untouched."""

        if hasattr(source, "read") and hasattr(source, "seek"):
            source.seek(0)
            return source
        return Path(source)

    @staticmethod
    def _portable_basename(value: str) -> str:
        """Return a basename for either Windows or POSIX-style legacy paths."""

        return PurePosixPath(str(value).replace("\\", "/")).name

    @staticmethod
    def _validate_history_rows(history_path: Path, *, context: str) -> list[dict]:
        """Read history rows with the same fail-closed semantics used at runtime."""

        rows: list[dict] = []
        try:
            with history_path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise BackupError(
                            f"{context}: cronologia danneggiata alla riga "
                            f"{line_number}"
                        ) from exc
                    if not isinstance(item, dict):
                        raise BackupError(
                            f"{context}: cronologia danneggiata alla riga "
                            f"{line_number}"
                        )
                    rows.append(item)
        except BackupError:
            raise
        except (OSError, UnicodeError) as exc:
            raise BackupError(f"{context}: cronologia non leggibile") from exc
        return rows

    @staticmethod
    def _sanitized_settings_bytes(settings_path: Path) -> bytes:
        """Serialize supported settings without machine-specific logo paths."""
        settings = SettingsStore(settings_path).load()
        logo_value = str(settings.get("logo_path", "") or "").strip()
        if logo_value:
            settings["logo_path"] = BackupService._portable_basename(logo_value)
        return json.dumps(
            settings,
            indent=2,
            ensure_ascii=False,
        ).encode("utf-8")

    @staticmethod
    def _sanitized_history_bytes(history_path: Path) -> bytes:
        """Normalize legacy absolute PDF paths while preserving audit events.

        HMAC identifiers and lifecycle timestamps are left untouched. Only
        output_file is reduced to its basename so a portable backup does not
        disclose the Windows profile path that created an older event.
        """
        rows: list[str] = []
        for item in BackupService._validate_history_rows(
            history_path,
            context="Impossibile creare il backup",
        ):
            output = str(item.get("output_file", "") or "").strip()
            if output:
                item["output_file"] = BackupService._portable_basename(output)
            rows.append(
                json.dumps(
                    item,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )

        payload = "\n".join(rows)
        if rows:
            payload += "\n"
        return payload.encode("utf-8")

    def create(
        self,
        destination: Path,
        *,
        password: str | None = None,
    ) -> Path:
        """Create a legacy ZIP or an optional password-protected .vmbk backup."""

        destination = Path(destination)
        self._assert_no_pending_print_audit()
        self._assert_no_pending_create()
        if password is None:
            return self._create_zip(destination)

        password = self._validated_backup_password(password)
        self._ensure_destination_outside_data_root(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)

        encrypted_temp = destination.with_suffix(destination.suffix + ".tmp")
        encrypted_temp.unlink(missing_ok=True)

        try:
            # zipfile supports unseekable writers. Feed the logical ZIP stream
            # directly into AES-GCM so encrypted backup creation never writes a
            # plaintext archive to disk.
            try:
                with ProtectedBackupWriter(
                    encrypted_temp,
                    password,
                ) as protected:
                    with zipfile.ZipFile(
                        protected,
                        "w",
                        compression=zipfile.ZIP_DEFLATED,
                    ) as archive:
                        self._write_archive_contents(archive)
            except (ProtectedBackupError, ValueError) as exc:
                raise BackupError(str(exc)) from exc

            with tempfile.TemporaryFile(mode="w+b") as check_zip:
                self._decrypt_to_stream(
                    encrypted_temp,
                    check_zip,
                    password,
                )
                self.validate(check_zip)

            os.replace(encrypted_temp, destination)
            return destination
        except Exception as exc:
            encrypted_temp.unlink(missing_ok=True)
            if isinstance(exc, BackupError):
                raise
            raise BackupError(
                "Creazione backup cifrato non riuscita"
            ) from exc

    def _write_archive_contents(self, archive: zipfile.ZipFile) -> None:
        """Write one verified logical backup into an already-open ZIP."""

        sqlite_snapshot = self._sqlite_snapshot_bytes()
        manifest = {
            "format": BACKUP_FORMAT,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "application": PRODUCT_NAME,
            "includes": list(self.DATA_DIRS),
            "portable_history_key": (
                self.paths.data / "history_secret.key"
            ).is_file(),
        }
        if sqlite_snapshot is not None:
            payload, user_version, digest = sqlite_snapshot
            manifest["sqlite_snapshot"] = {
                "member": SQLITE_MEMBER,
                "method": "sqlite3.Connection.backup",
                "integrity_check": "ok",
                "user_version": user_version,
                "sha256": digest,
            }

        archive.writestr(
            self.MANIFEST,
            json.dumps(manifest, indent=2, ensure_ascii=False),
        )
        if sqlite_snapshot is not None:
            archive.writestr(SQLITE_MEMBER, sqlite_snapshot[0])
        for dirname in self.DATA_DIRS:
            root = self.paths.user_root / dirname
            if not root.exists():
                continue
            for source in root.rglob("*"):
                if not source.is_file() or source.is_symlink():
                    continue
                if source.name in {
                    "history.lock",
                    "application.instance.lock",
                    "pending_print_audit.json",
                    "pending_print_audit.json.tmp",
                    "pending_create_guard",
                }:
                    # Lock/guard files describe live process state and are never
                    # portable application data.
                    continue
                if (
                    dirname == "Print"
                    and source.name.startswith(".Voucher_")
                    and source.suffix.lower() == ".tmp"
                ):
                    continue
                if (
                    dirname == "config"
                    and source.name.startswith("settings.json.corrupt-")
                ):
                    continue

                relative = source.relative_to(
                    self.paths.user_root
                ).as_posix()
                if relative == SQLITE_MEMBER or relative in SQLITE_SIDECAR_MEMBERS:
                    # The main database is archived only from the online SQLite
                    # snapshot above. WAL/SHM/journal files are never portable.
                    continue
                if relative == "config/settings.json":
                    archive.writestr(
                        relative,
                        self._sanitized_settings_bytes(source),
                    )
                elif relative == "data/history.jsonl":
                    archive.writestr(
                        relative,
                        self._sanitized_history_bytes(source),
                    )
                else:
                    archive.write(source, relative)

    def _ensure_destination_outside_data_root(
        self,
        destination: Path,
    ) -> None:
        """Reject backup destinations inside application-managed data."""

        try:
            inside_data_root = Path(destination).resolve().is_relative_to(
                self.paths.user_root.resolve()
            )
        except (OSError, RuntimeError):
            inside_data_root = False
        if inside_data_root:
            raise BackupError(
                "Salvare il backup fuori dalla cartella dati dell'applicazione"
            )

    def _create_zip(self, destination: Path) -> Path:
        """Write a validated ZIP snapshot and return its final path."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_destination_outside_data_root(destination)

        temp = destination.with_suffix(destination.suffix + ".tmp")
        try:
            with zipfile.ZipFile(
                temp,
                "w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                self._write_archive_contents(archive)
            # Validate the newly written temporary archive *before* replacing
            # an existing backup. A failed validation must never destroy the
            # last known-good backup at the destination path.
            self.validate(temp)
            os.replace(temp, destination)
            return destination
        except Exception as exc:
            temp.unlink(missing_ok=True)
            if isinstance(exc, BackupError):
                raise
            raise BackupError(
                f"Creazione backup non riuscita: {exc}"
            ) from exc

    @classmethod
    def _validated_member_name(cls, raw_name: str) -> PurePosixPath:
        """Return a Windows-safe archive-relative path or reject the member.

        Backups are restored on Windows, where case folding, device names,
        alternate-data-stream colons and trailing dots/spaces can make two ZIP
        names resolve to an unexpected filesystem target. Reject those forms
        before extraction rather than relying on platform-specific path cleanup.
        """
        normalized = raw_name.replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts:
            raise BackupError("Il backup contiene un percorso non sicuro")
        if not path.parts:
            raise BackupError("Il backup contiene un percorso vuoto")

        first = path.parts[0]
        if first not in cls.ALLOWED_ROOTS:
            raise BackupError("Il backup contiene dati non riconosciuti")

        for part in path.parts:
            if not part or part in {".", ".."}:
                raise BackupError("Il backup contiene un percorso non sicuro")
            if ":" in part or part.endswith((" ", ".")):
                raise BackupError("Il backup contiene un nome file non sicuro")
            device_base = part.split(".", 1)[0].upper()
            if device_base in WINDOWS_RESERVED_NAMES:
                raise BackupError("Il backup contiene un nome Windows riservato")
        return path

    def validate_encrypted(
        self,
        source: Path,
        password: str,
    ) -> dict:
        """Authenticate/decrypt into an anonymous temp file, then validate."""

        with tempfile.TemporaryFile(mode="w+b") as decrypted:
            self._decrypt_to_stream(
                source,
                decrypted,
                password,
            )
            return self.validate(decrypted)

    def validate(self, source: Path) -> dict:
        """Validate structure, supported format and extraction limits."""
        source = self._zip_source(source)
        try:
            with zipfile.ZipFile(source, "r") as archive:
                infos = archive.infolist()
                if len(infos) > MAX_ARCHIVE_FILES:
                    raise BackupError("Il backup contiene troppi file")

                names = {info.filename for info in infos}
                if self.MANIFEST not in names:
                    raise BackupError(
                        "Il file non è un backup Voucher Management valido"
                    )

                total_size = 0
                normalized_names: set[str] = set()
                for info in infos:
                    relative = self._validated_member_name(info.filename)
                    # Windows paths are case-insensitive. Reject duplicate or
                    # case-colliding members before extraction so archive order
                    # can never overwrite previously validated content.
                    normalized = "/".join(
                        part.rstrip(" .").casefold()
                        for part in relative.parts
                    )
                    if normalized in normalized_names:
                        raise BackupError(
                            "Il backup contiene percorsi duplicati"
                        )
                    normalized_names.add(normalized)

                    total_size += int(info.file_size)
                    if total_size > MAX_UNCOMPRESSED_BYTES:
                        raise BackupError(
                            "Il backup supera la dimensione massima supportata"
                        )

                manifest_info = next(
                    info for info in infos if info.filename == self.MANIFEST
                )
                if manifest_info.file_size > MAX_MANIFEST_BYTES:
                    raise BackupError("Manifest backup troppo grande")
                manifest = json.loads(
                    archive.read(self.MANIFEST).decode("utf-8")
                )
                if not isinstance(manifest, dict):
                    raise BackupError("Manifest backup non valido")
                backup_format = manifest.get("format")
                if (
                    type(backup_format) is not int
                    or backup_format not in SUPPORTED_BACKUP_FORMATS
                ):
                    raise BackupError(
                        "Versione del formato backup non supportata"
                    )
                if (
                    backup_format == BACKUP_FORMAT
                    and manifest.get("application") != PRODUCT_NAME
                ):
                    raise BackupError(
                        "Il backup non appartiene a Voucher Management"
                    )

                if names.intersection(SQLITE_SIDECAR_MEMBERS):
                    raise BackupError(
                        "Il backup contiene file SQLite WAL/SHM non portabili"
                    )

                sqlite_meta = manifest.get("sqlite_snapshot")
                has_sqlite = SQLITE_MEMBER in names
                if sqlite_meta is not None:
                    if not isinstance(sqlite_meta, dict) or not has_sqlite:
                        raise BackupError(
                            "Metadati snapshot SQLite incoerenti"
                        )
                    if sqlite_meta.get("member") != SQLITE_MEMBER:
                        raise BackupError(
                            "Metadati snapshot SQLite non validi"
                        )
                    if sqlite_meta.get("method") != "sqlite3.Connection.backup":
                        raise BackupError(
                            "Metodo snapshot SQLite non riconosciuto"
                        )
                    payload = archive.read(SQLITE_MEMBER)
                    user_version = self._sqlite_integrity_from_bytes(payload)
                    expected_hash = str(
                        sqlite_meta.get("sha256", "")
                    ).strip().lower()
                    if (
                        len(expected_hash) != 64
                        or hashlib.sha256(payload).hexdigest() != expected_hash
                    ):
                        raise BackupError(
                            "Hash dello snapshot SQLite non corrispondente"
                        )
                    if sqlite_meta.get("integrity_check") != "ok":
                        raise BackupError(
                            "Snapshot SQLite non dichiarato integro"
                        )
                    if sqlite_meta.get("user_version") != user_version:
                        raise BackupError(
                            "Versione schema SQLite del backup non coerente"
                        )
                elif has_sqlite:
                    # Early 5.0 beta archives copied the live .db directly.
                    # They cannot prove that committed WAL pages were captured,
                    # even when the main file itself passes integrity_check.
                    raise BackupError(
                        "Backup SQLite privo di metadati snapshot verificabili"
                    )
                return manifest
        except (
            zipfile.BadZipFile,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ) as exc:
            raise BackupError(
                "Backup danneggiato o non riconosciuto"
            ) from exc

    def _extract_validated(self, source, staging: Path) -> dict:
        manifest = self.validate(source)
        source = self._zip_source(source)
        with zipfile.ZipFile(source, "r") as archive:
            for info in archive.infolist():
                relative = self._validated_member_name(info.filename)
                target = staging.joinpath(*relative.parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        return manifest

    def _validate_staged_logos(self, staging: Path) -> None:
        """Validate restored logos before any live application data is changed.

        A valid PNG/JPEG that only exceeds the current size policy is omitted
        for backward compatibility. Unsupported, disguised or corrupt image
        data still invalidates the backup.
        """

        logos = staging / "Loghi"
        if not logos.is_dir():
            return

        settings_path = staging / "config" / "settings.json"
        settings = SettingsStore(settings_path).load()
        configured = self._portable_basename(
            str(settings.get("logo_path", "") or "").strip()
        ).casefold()
        settings_changed = False

        for logo in logos.rglob("*"):
            if not logo.is_file() or logo.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
                continue
            try:
                validate_logo_image(logo)
            except LogoLimitError as exc:
                logo.unlink(missing_ok=True)
                self._restore_warnings.append(f"{logo.name}: {exc}")
                if configured and configured == logo.name.casefold():
                    settings["logo_path"] = ""
                    settings_changed = True
            except LogoValidationError as exc:
                raise BackupError(
                    f"Il backup contiene un logo non valido: {logo.name}"
                ) from exc

        if settings_changed:
            SettingsStore(settings_path).save(settings)

    @staticmethod
    def _validate_staged_history_identity(staging: Path) -> None:
        """Reject backups whose settings/history key cannot verify each other."""

        settings_path = staging / "config" / "settings.json"
        settings = SettingsStore(settings_path).load()
        expected = str(settings.get("history_key_fingerprint", "") or "").strip()
        secret = HistoryKeyStore(staging).get()
        history_path = staging / "data" / "history.jsonl"
        try:
            has_history = history_path.is_file() and history_path.stat().st_size > 0
        except OSError as exc:
            raise BackupError("Cronologia backup non leggibile") from exc

        if history_path.is_file():
            BackupService._validate_history_rows(
                history_path,
                context="Backup non valido",
            )

        actual = (
            hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]
            if secret
            else ""
        )
        if has_history and (not expected or not secret or actual != expected):
            raise BackupError(
                "Backup incoerente: cronologia, fingerprint e chiave locale "
                "non corrispondono"
            )
        if expected and (not secret or actual != expected):
            raise BackupError(
                "Backup incoerente: fingerprint della cronologia non verificabile"
            )

        # A backup must never pre-authorize a controller target/certificate.
        # Force the operator through the normal trust flow after restore.
        settings["controller_api_root"] = ""
        settings["controller_cert_sha256"] = ""
        SettingsStore(settings_path).save(settings)

    @staticmethod
    def _restore_v1_history_key(staging: Path, target_root: Path) -> None:
        """Map beta format-1 key material into the current storage layout."""
        legacy = staging / "security" / "history_secret.bin"
        if not legacy.is_file():
            return
        payload = legacy.read_bytes()
        current_key = target_root / "data" / "history_secret.key"
        prefixes = (
            HistoryKeyStore.PORTABLE_PREFIX,
            HistoryKeyStore.LEGACY_PORTABLE_PREFIX,
        )
        if payload.startswith(prefixes):
            current_key.parent.mkdir(parents=True, exist_ok=True)
            current_key.write_bytes(payload)
        else:
            # Old DPAPI blobs are retained only for one-time migration on the
            # Windows profile that can decrypt them.
            (target_root / "history_secret.bin").write_bytes(payload)

    def restore(
        self,
        source: Path,
        *,
        password: str | None = None,
    ) -> Path:
        """Restore either a legacy ZIP or an authenticated encrypted backup."""

        source = Path(source)
        self._assert_no_pending_print_audit()
        self._assert_no_pending_create()
        if not self.is_encrypted_backup(source):
            return self._restore_zip(source)
        if password is None:
            raise BackupError("Il backup cifrato richiede una password")

        with tempfile.TemporaryFile(mode="w+b") as decrypted:
            self._decrypt_to_stream(
                source,
                decrypted,
                password,
            )
            # Structural validation occurs before _restore_zip creates the
            # rollback snapshot or touches live application data.
            self.validate(decrypted)
            return self._restore_zip(decrypted)

    def _restore_zip(self, source) -> Path:
        """Restore a validated snapshot with a *complete* rollback prerequisite.

        The live data tree is never modified until a full rollback snapshot has
        been copied successfully. The snapshot is built under a temporary name
        and atomically renamed to its final rollback path only after copytree()
        completes. A disk-full/interrupted rollback copy therefore cannot be
        mistaken for a usable rollback source.
        """
        self._restore_warnings.clear()
        parent = self.paths.user_root.parent
        rollback = parent / (
            f"{PRODUCT_DIR_NAME}-rollback-"
            f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
        )
        staging = Path(
            tempfile.mkdtemp(prefix="voucher-management-restore-", dir=parent)
        )
        rollback_build_root = Path(
            tempfile.mkdtemp(
                prefix="voucher-management-rollback-build-",
                dir=parent,
            )
        )
        rollback_build = rollback_build_root / "snapshot"
        had_existing_root = self.paths.user_root.exists()
        rollback_ready = False

        try:
            manifest = self._extract_validated(source, staging)
            if not (staging / "config" / "settings.json").is_file():
                raise BackupError(
                    "Il backup non contiene la configurazione"
                )

            if manifest.get("format") == 1:
                self._restore_v1_history_key(staging, staging)
            self._validate_staged_logos(staging)
            self._validate_staged_history_identity(staging)

            if had_existing_root:
                # The rollback must be safe under WAL for the same reason as a
                # portable backup. Copy non-SQLite data normally, then inject a
                # verified online snapshot instead of copying live DB sidecars.
                rollback_sqlite = self._sqlite_snapshot_bytes()
                self._copytree_without_live_sqlite(
                    self.paths.user_root,
                    rollback_build,
                )
                if rollback_sqlite is not None:
                    rollback_db = (
                        rollback_build / "data" / "voucher_management.db"
                    )
                    rollback_db.parent.mkdir(parents=True, exist_ok=True)
                    rollback_db.write_bytes(rollback_sqlite[0])
                    self._sqlite_integrity_from_bytes(
                        rollback_db.read_bytes()
                    )
                os.replace(rollback_build, rollback)
                rollback_ready = True

            for dirname in self.DATA_DIRS:
                target = self.paths.user_root / dirname
                incoming = staging / dirname
                if target.exists():
                    shutil.rmtree(target)
                if incoming.exists():
                    shutil.copytree(incoming, target)
                else:
                    target.mkdir(parents=True, exist_ok=True)

            return rollback
        except Exception as exc:
            if rollback_ready and rollback.exists():
                # Never remove the whole user root: the diagnostic logger may
                # still hold an open file on Windows. Restore only managed data
                # directories, leaving logs and open handles untouched.
                for dirname in self.DATA_DIRS:
                    target = self.paths.user_root / dirname
                    saved = rollback / dirname
                    if target.exists():
                        shutil.rmtree(target)
                    if saved.exists():
                        shutil.copytree(saved, target)
                    else:
                        target.mkdir(parents=True, exist_ok=True)
                shutil.rmtree(rollback, ignore_errors=True)
            elif not had_existing_root and self.paths.user_root.exists():
                for dirname in self.DATA_DIRS:
                    target = self.paths.user_root / dirname
                    if target.exists():
                        shutil.rmtree(target)

            if isinstance(exc, BackupError):
                raise
            raise BackupError(
                f"Ripristino backup non riuscito: {exc}"
            ) from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(rollback_build_root, ignore_errors=True)
