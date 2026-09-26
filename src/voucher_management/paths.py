"""Program and persistent-data path management with beta migration support."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from .identity import LEGACY_PRODUCT_DIR_NAMES, PRODUCT_DIR_NAME
from .logo_validation import LogoValidationError, validate_logo_image


class AppPaths:
    """Resolve immutable program files separately from persistent user data.

    Portable/source execution remains per-user. An installed deployment opts
    into shared ProgramData only through a small non-secret deployment marker
    written beside the executable by the elevated Windows installer.

    Public releases use a neutral VoucherManagement data root. Existing beta
    data is imported from the legacy UniFiVoucherTool root without overwriting
    newer files, so the product rename does not discard settings, audit history,
    PDFs or custom logos.
    """

    DEPLOYMENT_MARKER = "voucher-management-deployment.json"
    SHARED_MODE = "shared_programdata"

    def __init__(
        self,
        *,
        base_override: Path | None = None,
        shared_root_override: Path | None = None,
    ) -> None:
        if base_override is not None:
            self.base = Path(base_override)
        elif getattr(sys, "frozen", False):
            self.base = Path(sys.executable).resolve().parent
        else:
            self.base = Path(__file__).resolve().parents[2]

        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            profile_root = Path(local_appdata)
            self.per_user_root = profile_root / PRODUCT_DIR_NAME
            self.legacy_profile_roots = tuple(
                profile_root / name for name in LEGACY_PRODUCT_DIR_NAMES
            )
        else:
            self.per_user_root = Path.home() / ".voucher-management"
            self.legacy_profile_roots = tuple(
                Path.home() / f".{name.lower()}"
                for name in LEGACY_PRODUCT_DIR_NAMES
            )

        marker_mode = self._deployment_mode()
        self.shared_mode = (
            shared_root_override is not None
            or marker_mode == self.SHARED_MODE
        )
        if self.shared_mode:
            if shared_root_override is not None:
                self.user_root = Path(shared_root_override)
            else:
                program_data = os.environ.get("PROGRAMDATA")
                if not program_data:
                    raise RuntimeError(
                        "Installazione condivisa non valida: PROGRAMDATA mancante"
                    )
                self.user_root = Path(program_data) / PRODUCT_DIR_NAME
            # These roots are migration candidates only. Shared mode never
            # imports them implicitly during ensure_writable().
            self.legacy_user_roots = (
                self.per_user_root,
                *self.legacy_profile_roots,
            )
        else:
            self.user_root = self.per_user_root
            self.legacy_user_roots = self.legacy_profile_roots

        self.config = self.user_root / "config"
        self.data = self.user_root / "data"
        self.logs = self.user_root / "logs"
        self.logos = self.user_root / "Loghi"
        self.prints = self.user_root / "Print"

        self.assets = self.base / "assets"
        self.history = self.data / "history.jsonl"
        self.history_lock = self.data / "history.lock"
        # Keep the lifetime guard outside config/data/Print/Loghi because
        # backup restore replaces those managed directories. In shared mode
        # this root is ProgramData, so the same OS file lock is visible across
        # Fast User Switching sessions.
        self.instance_lock = self.user_root / "application.instance.lock"
        self.pending_create = self.data / "pending_create_guard"
        self.database = self.data / "voucher_management.db"
        self.settings = self.config / "settings.json"
        self._logo_warning = ""

    def _deployment_mode(self) -> str:
        """Read the installer-owned deployment marker without side effects."""

        marker = self.base / self.DEPLOYMENT_MARKER
        if not marker.exists():
            return ""
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "Marker di installazione Voucher Management non valido"
            ) from exc
        if not isinstance(payload, dict) or payload.get("format") != 1:
            raise RuntimeError(
                "Marker di installazione Voucher Management non supportato"
            )
        mode = str(payload.get("mode", "") or "").strip()
        if mode not in {"", self.SHARED_MODE}:
            raise RuntimeError(
                "Modalità di installazione Voucher Management non supportata"
            )
        return mode

    @staticmethod
    def _copy_file_if_missing(source: Path, target: Path) -> None:
        if source.is_file() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    @staticmethod
    def _merge_directory(source: Path, target: Path) -> None:
        """Copy only files absent from the destination tree."""
        if not source.is_dir():
            return
        target.mkdir(parents=True, exist_ok=True)
        for item in source.rglob("*"):
            if item.is_symlink() or not item.is_file():
                continue
            relative = item.relative_to(source)
            destination = target / relative
            if destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)

    def _migrate_legacy_user_roots(self) -> None:
        """Import persistent data created under the previous product identity."""
        for legacy in self.legacy_user_roots:
            if not legacy.exists() or legacy.resolve() == self.user_root.resolve():
                continue
            self._copy_file_if_missing(
                legacy / "config" / "settings.json", self.settings
            )
            self._copy_file_if_missing(
                legacy / "data" / "history.jsonl", self.history
            )
            self._copy_file_if_missing(
                legacy / "data" / "history_secret.key",
                self.data / "history_secret.key",
            )
            self._merge_directory(legacy / "Print", self.prints)
            self._merge_directory(legacy / "Loghi", self.logos)

    def _migrate_legacy_portable_data(self) -> None:
        """Import pre-4.1 data stored beside the executable."""
        self._copy_file_if_missing(
            self.base / "config" / "settings.json", self.settings
        )
        self._copy_file_if_missing(
            self.base / "data" / "history.jsonl", self.history
        )
        self._copy_file_if_missing(
            self.base / "data" / "history_secret.key",
            self.data / "history_secret.key",
        )
        self._merge_directory(self.base / "Print", self.prints)

        legacy_assets = self.base / "assets"
        if legacy_assets.is_dir():
            for source in legacy_assets.glob("custom_logo.*"):
                if source.is_symlink() or not source.is_file():
                    continue
                target = self.logos / source.name
                if not target.exists():
                    shutil.copy2(source, target)

    def consume_logo_warning(self) -> str:
        warning = self._logo_warning
        self._logo_warning = ""
        return warning

    def persist_configured_logo(self, configured_path: str) -> str:
        """Import a configured logo into persistent application data.

        Older betas could point settings at the extracted program directory or
        an external/network path. Only logos that satisfy the current PNG/JPEG
        validation policy are imported or reused.
        """
        value = str(configured_path or "").strip()
        if not value:
            return ""

        source = Path(value).expanduser()
        candidate = self.logos / source.name

        if source.is_file() and not source.is_symlink():
            try:
                validate_logo_image(source)

                if source.resolve() == candidate.resolve():
                    return str(candidate)

                if candidate.is_file():
                    try:
                        validate_logo_image(candidate)
                        return str(candidate)
                    except LogoValidationError:
                        candidate.unlink(missing_ok=True)

                self.logos.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, candidate)
                return str(candidate)
            except LogoValidationError:
                self._logo_warning = (
                    "Il logo configurato non è più valido o supera i limiti "
                    "supportati ed è stato rimosso dalla configurazione."
                )
                return ""
            except OSError:
                # A legacy logo may live on a temporarily unavailable share.
                # Keep the configured value; rendering will revalidate it
                # before ReportLab can decode anything.
                return value

        if candidate.is_file():
            try:
                validate_logo_image(candidate)
            except LogoValidationError:
                self._logo_warning = (
                    "Il logo salvato nella libreria locale non è più valido "
                    "ed è stato rimosso dalla configurazione."
                )
                return ""
            return str(candidate)

        return value

    def ensure_writable(self) -> None:
        for folder in (self.config, self.data, self.logs, self.logos, self.prints):
            folder.mkdir(parents=True, exist_ok=True)

        if not self.shared_mode:
            # Portable/per-user compatibility can safely retain the historical
            # implicit migration. Shared ProgramData requires an explicit
            # operator-controlled migration because several Windows profiles
            # may contain independent histories.
            self._migrate_legacy_user_roots()
            self._migrate_legacy_portable_data()

        for folder in (self.config, self.data, self.logs, self.logos, self.prints):
            probe = folder / f".write-test-{os.getpid()}"
            try:
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
            except OSError as exc:
                raise PermissionError(f"Cartella non scrivibile: {folder}") from exc
