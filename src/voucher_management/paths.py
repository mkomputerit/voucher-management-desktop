"""Program and persistent-data path management with beta migration support."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .identity import LEGACY_PRODUCT_DIR_NAMES, PRODUCT_DIR_NAME
from .logo_validation import LogoValidationError, validate_logo_image


class AppPaths:
    """Resolve immutable program files separately from persistent user data.

    Public releases use a neutral VoucherManagement data root. Existing beta
    data is imported from the legacy UniFiVoucherTool root without overwriting
    newer files, so the product rename does not discard settings, audit history,
    PDFs or custom logos.
    """

    def __init__(self) -> None:
        if getattr(sys, "frozen", False):
            self.base = Path(sys.executable).resolve().parent
        else:
            self.base = Path(__file__).resolve().parents[2]

        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            profile_root = Path(local_appdata)
            self.user_root = profile_root / PRODUCT_DIR_NAME
            self.legacy_user_roots = tuple(
                profile_root / name for name in LEGACY_PRODUCT_DIR_NAMES
            )
        else:
            self.user_root = Path.home() / ".voucher-management"
            self.legacy_user_roots = tuple(
                Path.home() / f".{name.lower()}" for name in LEGACY_PRODUCT_DIR_NAMES
            )

        self.config = self.user_root / "config"
        self.data = self.user_root / "data"
        self.logs = self.user_root / "logs"
        self.logos = self.user_root / "Loghi"
        self.prints = self.user_root / "Print"

        self.assets = self.base / "assets"
        self.history = self.data / "history.jsonl"
        self.history_lock = self.data / "history.lock"
        self.pending_create = self.data / "pending_create_guard"
        self.settings = self.config / "settings.json"
        self._logo_warning = ""

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

        self._migrate_legacy_user_roots()
        self._migrate_legacy_portable_data()

        for folder in (self.config, self.data, self.logs, self.logos, self.prints):
            probe = folder / f".write-test-{os.getpid()}"
            try:
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
            except OSError as exc:
                raise PermissionError(f"Cartella non scrivibile: {folder}") from exc
