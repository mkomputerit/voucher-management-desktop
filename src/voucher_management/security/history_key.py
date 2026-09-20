"""Portable HMAC history-key storage and legacy migration."""

from __future__ import annotations

import base64
import os
from pathlib import Path

from ..identity import LEGACY_PRODUCT_DIR_NAMES, PRODUCT_DIR_NAME


class HistoryKeyError(RuntimeError):
    """Raised when legacy key material cannot be decoded."""


def _fallback_unprotect(data: bytes) -> bytes:
    """Decode non-Windows legacy fixtures used by automated tests."""
    if not data.startswith(b"PLAIN:"):
        raise HistoryKeyError("Formato secret legacy non valido")
    return base64.b64decode(data[6:])


def _unprotect_legacy_dpapi(data: bytes) -> bytes:
    """Decrypt a legacy Windows DPAPI blob for one-time migration only."""
    if os.name != "nt":
        return _fallback_unprotect(data)

    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    buffer = ctypes.create_string_buffer(data)
    in_blob = DATA_BLOB(
        len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    )
    out_blob = DATA_BLOB()

    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    ):
        raise HistoryKeyError("CryptUnprotectData non riuscito")

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


class HistoryKeyStore:
    """Persist the HMAC history identity as portable application data.

    The stored value is a random local application secret, never a controller
    password or API key. It lives beside history.jsonl so a complete backup can
    be restored under another Windows account without breaking print-history
    correlation.

    Older DPAPI blobs are supported only as a one-time migration path.
    """

    PORTABLE_PREFIX = b"VM-HISTORY-KEY-V1:"
    LEGACY_PORTABLE_PREFIX = b"UVT-HISTORY-KEY-V1:"

    def __init__(
        self,
        root: Path | None = None,
        *,
        legacy_roots: tuple[Path, ...] | list[Path] | None = None,
        filename: str = "history_secret.key",
    ):
        inferred_legacy: tuple[Path, ...] = ()
        if root is None:
            if os.name == "nt":
                profile_root = Path(
                    os.environ.get(
                        "LOCALAPPDATA",
                        Path.home() / "AppData" / "Local",
                    )
                )
                root = profile_root / PRODUCT_DIR_NAME
                inferred_legacy = tuple(
                    profile_root / name for name in LEGACY_PRODUCT_DIR_NAMES
                )
            else:
                root = Path.home() / ".voucher-management"

        self.root = Path(root)
        self.path = self.root / "data" / filename

        candidates = (
            self.root,
            *inferred_legacy,
            *(Path(p) for p in (legacy_roots or ())),
        )
        unique_roots: list[Path] = []
        for candidate in candidates:
            if candidate not in unique_roots:
                unique_roots.append(candidate)
        self.legacy_paths = tuple(
            candidate / "history_secret.bin" for candidate in unique_roots
        )

    def set(self, secret: str) -> None:
        """Atomically store a minimally valid portable history identity."""
        if not isinstance(secret, str) or len(secret.strip()) < 16:
            raise HistoryKeyError(
                "Chiave cronologia troppo corta o non valida"
            )
        secret = secret.strip()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.PORTABLE_PREFIX + base64.b64encode(
            secret.encode("utf-8")
        )
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with temp.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def _read_portable(self) -> str | None:
        if not self.path.exists():
            return None
        try:
            payload = self.path.read_bytes()
            if payload.startswith(self.PORTABLE_PREFIX):
                encoded = payload[len(self.PORTABLE_PREFIX):]
            elif payload.startswith(self.LEGACY_PORTABLE_PREFIX):
                encoded = payload[len(self.LEGACY_PORTABLE_PREFIX):]
            else:
                return None

            secret = base64.b64decode(
                encoded,
                validate=True,
            ).decode("utf-8").strip()
            if len(secret) < 16:
                return None

            if payload.startswith(self.LEGACY_PORTABLE_PREFIX):
                # Preserve beta history correlation while completing rebrand.
                self.set(secret)
            return secret
        except (OSError, ValueError, UnicodeDecodeError):
            return None

    def _migrate_legacy_dpapi(self) -> str | None:
        for legacy_path in self.legacy_paths:
            if not legacy_path.exists():
                continue
            try:
                secret = _unprotect_legacy_dpapi(
                    legacy_path.read_bytes()
                ).decode("utf-8")
            except (OSError, HistoryKeyError, UnicodeDecodeError):
                continue
            if secret:
                self.set(secret)
                return secret
        return None

    def get(self) -> str | None:
        return self._read_portable() or self._migrate_legacy_dpapi()

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
