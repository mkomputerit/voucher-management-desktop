"""Whitelisted persistent settings; authentication secrets are never schema fields."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from copy import deepcopy
from pathlib import Path
from tempfile import NamedTemporaryFile

from .identity import (
    DEFAULT_STRUCTURE_NAME,
    DEFAULT_STRUCTURE_TYPE,
    DEFAULT_WIFI_TITLE,
)

DEFAULT_SETTINGS = {
    "structure_type": DEFAULT_STRUCTURE_TYPE,
    "structure_name": DEFAULT_STRUCTURE_NAME,
    "wifi_title": DEFAULT_WIFI_TITLE,
    "location": "",
    "preset": "Classico",
    "logo_path": "",
    # Prefer the exact root copied from UniFi Network > Integrations. The value
    # is not a credential and may therefore be persisted/backed up.
    "controller_api_root": "",
    # Optional SHA-256 pin for an explicitly trusted local/self-signed
    # controller certificate. This is not a credential.
    "controller_cert_sha256": "",
    "ui_theme": "system",
    "history_key_fingerprint": "",
    "log_retention_days": 30,
    "print_retention_days": 0,
}


_INT_SETTING_RANGES = {
    "log_retention_days": (1, 3650),
    "print_retention_days": (0, 3650),
}


class SettingsStore:
    """Atomic JSON settings store with neutral, non-operational defaults."""

    @staticmethod
    def _validated_value(key: str, value):
        """Return a schema-safe value, rejecting bools and out-of-range ints."""

        bounds = _INT_SETTING_RANGES.get(key)
        if bounds is None:
            return value, False

        minimum, maximum = bounds
        if type(value) is int and minimum <= value <= maximum:
            return value, False
        return DEFAULT_SETTINGS[key], True

    def __init__(self, path: Path):
        self.path = Path(path)
        self.last_load_warning = ""

    def _preserve_corrupt_file(self) -> Path | None:
        """Keep one forensic copy per distinct malformed settings payload."""

        if not self.path.is_file():
            return None
        try:
            payload = self.path.read_bytes()
        except OSError:
            return None

        digest = hashlib.sha256(payload).hexdigest()[:16]
        target = self.path.with_name(
            f"{self.path.name}.corrupt-{digest}"
        )
        if target.is_file():
            return target

        try:
            shutil.copy2(self.path, target)
            return target
        except OSError:
            return None

    def consume_warning(self) -> str:
        warning = self.last_load_warning
        self.last_load_warning = ""
        return warning

    def load(self) -> dict:
        self.last_load_warning = ""
        settings = deepcopy(DEFAULT_SETTINGS)
        if not self.path.exists():
            return settings
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            preserved = self._preserve_corrupt_file()
            self.last_load_warning = (
                "Il file impostazioni è danneggiato. È stata conservata una "
                "copia prima di usare valori sicuri predefiniti."
                if preserved is not None
                else
                "Il file impostazioni è danneggiato e non è stato possibile "
                "salvarne una copia. Sono stati caricati valori sicuri "
                "predefiniti."
            )
            return settings
        except OSError:
            self.last_load_warning = (
                "Impossibile leggere il file impostazioni. Sono stati caricati "
                "valori sicuri predefiniti."
            )
            return settings
        if isinstance(payload, dict):
            # Accept only the documented schema. This deliberately drops
            # unknown legacy keys so an accidentally persisted credential-like
            # value can never be carried forward into a new settings save or
            # application backup.
            invalid_keys: list[str] = []
            for key in DEFAULT_SETTINGS:
                if key in payload:
                    value, invalid = self._validated_value(
                        key,
                        payload[key],
                    )
                    settings[key] = value
                    if invalid:
                        invalid_keys.append(key)

            if invalid_keys:
                self.last_load_warning = (
                    "Alcune impostazioni numeriche non erano valide e sono "
                    "state ripristinate a valori sicuri: "
                    + ", ".join(sorted(invalid_keys))
                    + "."
                )

            # Private betas stored only the controller host/IP. Import it once
            # into the new official-API field; save() then writes only the new
            # schema and removes remembered usernames from future backups.
            if not settings["controller_api_root"]:
                legacy_host = str(payload.get("cloud_key_host", "")).strip()
                if legacy_host:
                    settings["controller_api_root"] = legacy_host
        return settings

    def save(self, settings: dict) -> None:
        """Persist settings atomically so interrupted writes do not corrupt them."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = deepcopy(DEFAULT_SETTINGS)
        # Persist only the documented schema even if a caller supplies an
        # extended dictionary.
        for key in DEFAULT_SETTINGS:
            if key in settings:
                value, _invalid = self._validated_value(
                    key,
                    settings[key],
                )
                payload[key] = value
        temp_name = None
        try:
            with NamedTemporaryFile(
                "w",
                encoding="utf-8",
                delete=False,
                dir=self.path.parent,
                suffix=".tmp",
            ) as tmp:
                json.dump(payload, tmp, indent=2, ensure_ascii=False)
                tmp.flush()
                os.fsync(tmp.fileno())
                temp_name = tmp.name
            os.replace(temp_name, self.path)
        finally:
            if temp_name and Path(temp_name).exists():
                Path(temp_name).unlink(missing_ok=True)


    def update(self, **changes) -> dict:
        """Merge whitelisted changes into the latest on-disk state atomically.

        Callers must not save a long-lived settings dictionary because another
        service (notably HistoryService) can update safety-critical fields such
        as the history-key fingerprint between UI operations.
        """

        unknown = set(changes) - set(DEFAULT_SETTINGS)
        if unknown:
            raise KeyError(
                "Impostazioni non riconosciute: "
                + ", ".join(sorted(unknown))
            )
        settings = self.load()
        for key, value in changes.items():
            validated, _invalid = self._validated_value(key, value)
            settings[key] = validated
        self.save(settings)
        return settings
