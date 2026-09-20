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
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .identity import PRODUCT_DIR_NAME, PRODUCT_NAME
from .security.history_key import HistoryKeyStore
from .settings import SettingsStore


BACKUP_FORMAT = 2
SUPPORTED_BACKUP_FORMATS = {1, BACKUP_FORMAT}
MAX_ARCHIVE_FILES = 10_000
MAX_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024

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

    @staticmethod
    def _sanitized_settings_bytes(settings_path: Path) -> bytes:
        """Serialize supported settings without machine-specific logo paths."""
        settings = SettingsStore(settings_path).load()
        logo_value = str(settings.get("logo_path", "") or "").strip()
        if logo_value:
            settings["logo_path"] = Path(logo_value).name
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
        try:
            with history_path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise BackupError(
                            "Impossibile creare un backup da una cronologia "
                            f"danneggiata alla riga {line_number}"
                        ) from exc
                    if not isinstance(item, dict):
                        raise BackupError(
                            "Impossibile creare un backup da una cronologia "
                            f"danneggiata alla riga {line_number}"
                        )
                    output = str(item.get("output_file", "") or "").strip()
                    if output:
                        item["output_file"] = Path(output).name
                    rows.append(
                        json.dumps(
                            item,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
        except BackupError:
            raise
        except (OSError, UnicodeError) as exc:
            raise BackupError(
                "Impossibile leggere la cronologia durante il backup"
            ) from exc

        payload = "\n".join(rows)
        if rows:
            payload += "\n"
        return payload.encode("utf-8")
    def create(self, destination: Path) -> Path:
        """Write a validated ZIP snapshot and return its final path."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)

        try:
            inside_data_root = destination.resolve().is_relative_to(
                self.paths.user_root.resolve()
            )
        except (OSError, RuntimeError):
            inside_data_root = False
        if inside_data_root:
            raise BackupError(
                "Salvare il backup fuori dalla cartella dati dell'applicazione"
            )

        manifest = {
            "format": BACKUP_FORMAT,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "application": PRODUCT_NAME,
            "includes": list(self.DATA_DIRS),
            "portable_history_key": (
                self.paths.data / "history_secret.key"
            ).is_file(),
        }
        temp = destination.with_suffix(destination.suffix + ".tmp")
        try:
            with zipfile.ZipFile(
                temp, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                archive.writestr(
                    self.MANIFEST,
                    json.dumps(manifest, indent=2, ensure_ascii=False),
                )
                for dirname in self.DATA_DIRS:
                    root = self.paths.user_root / dirname
                    if not root.exists():
                        continue
                    for source in root.rglob("*"):
                        if not source.is_file() or source.is_symlink():
                            continue
                        if source.name == "history.lock":
                            continue

                        relative = source.relative_to(
                            self.paths.user_root
                        ).as_posix()
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

    def validate(self, source: Path) -> dict:
        """Validate structure, supported format and extraction limits."""
        source = Path(source)
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
                return manifest
        except (
            zipfile.BadZipFile,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ) as exc:
            raise BackupError(
                "Backup danneggiato o non riconosciuto"
            ) from exc

    def _extract_validated(self, source: Path, staging: Path) -> dict:
        manifest = self.validate(source)
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

    def restore(self, source: Path) -> Path:
        """Restore a validated snapshot with a *complete* rollback prerequisite.

        The live data tree is never modified until a full rollback snapshot has
        been copied successfully. The snapshot is built under a temporary name
        and atomically renamed to its final rollback path only after copytree()
        completes. A disk-full/interrupted rollback copy therefore cannot be
        mistaken for a usable rollback source.
        """
        source = Path(source)
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
            self._validate_staged_history_identity(staging)

            if had_existing_root:
                # Do not touch user_root until this copy is complete. The final
                # rename is on the same filesystem and makes rollback existence
                # a reliable "snapshot complete" signal.
                shutil.copytree(self.paths.user_root, rollback_build)
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
