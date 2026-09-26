"""Password-protected backup container primitives.

The protected container wraps the existing validated ZIP backup rather than
inventing a second archive format. Encryption is streaming AES-256-GCM and the
key is derived from the operator password with scrypt. Header bytes are
authenticated as associated data so KDF salt, nonce and format version cannot
be modified without detection.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


PROTECTED_BACKUP_MAGIC = b"VMBACKUP\x00"
PROTECTED_BACKUP_VERSION = 1
SALT_BYTES = 16
NONCE_BYTES = 12
TAG_BYTES = 16
KEY_BYTES = 32
CHUNK_BYTES = 1024 * 1024
MIN_PASSWORD_CHARS = 12
MAX_PASSWORD_CHARS = 1024

LOGGER = logging.getLogger("voucher_management.backup_crypto")

# ~128 MiB memory cost. Parameters are fixed by container version so untrusted
# backup metadata cannot request attacker-controlled KDF resource usage.
SCRYPT_N = 2**17
SCRYPT_R = 8
SCRYPT_P = 1

HEADER_BYTES = (
    len(PROTECTED_BACKUP_MAGIC)
    + 1
    + SALT_BYTES
    + NONCE_BYTES
)


class ProtectedBackupError(RuntimeError):
    """Raised when a protected backup container is structurally invalid."""


class ProtectedBackupAuthenticationError(ProtectedBackupError):
    """Raised when password verification or GCM authentication fails."""


class ProtectedBackupWriter:
    """Unseekable file-like object that encrypts ZIP bytes as they are written."""

    def __init__(self, destination: Path, password: str):
        self.destination = Path(destination)
        self.password = validate_backup_password(password)
        self._target = None
        self._encryptor = None
        self._plain_position = 0
        self._closed = False

    def __enter__(self):
        salt = os.urandom(SALT_BYTES)
        nonce = os.urandom(NONCE_BYTES)
        header = _header(salt, nonce)
        key = _derive_key(self.password, salt)

        self.destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._target = self.destination.open("wb")
            self._target.write(header)
            self._encryptor = Cipher(
                algorithms.AES(key),
                modes.GCM(nonce),
            ).encryptor()
            self._encryptor.authenticate_additional_data(header)
            return self
        except Exception:
            if self._target is not None:
                self._target.close()
            self.destination.unlink(missing_ok=True)
            raise

    def write(self, data: bytes) -> int:
        if self._closed or self._target is None or self._encryptor is None:
            raise ValueError("I/O operation on closed protected backup")
        payload = bytes(data)
        if payload:
            self._target.write(self._encryptor.update(payload))
            self._plain_position += len(payload)
        return len(payload)

    def tell(self) -> int:
        return self._plain_position

    def flush(self) -> None:
        if self._target is not None:
            self._target.flush()

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        target = self._target
        encryptor = self._encryptor
        self._target = None
        self._encryptor = None
        if target is None or encryptor is None:
            return
        try:
            target.write(encryptor.finalize())
            target.write(encryptor.tag)
            target.flush()
            os.fsync(target.fileno())
        finally:
            target.close()

    @property
    def closed(self) -> bool:
        return self._closed

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            try:
                if self._target is not None:
                    self._target.close()
            finally:
                self._target = None
                self._encryptor = None
                self._closed = True
                self.destination.unlink(missing_ok=True)
            return False

        try:
            self.close()
        except Exception:
            self.destination.unlink(missing_ok=True)
            raise
        return False


def validate_backup_password(password: str) -> str:
    """Validate an operator-supplied password without normalizing it.

    Password bytes are derived from the exact UTF-8 text entered by the
    operator. Leading/trailing spaces are therefore significant and are not
    stripped silently.
    """

    if not isinstance(password, str):
        raise ValueError("Password backup non valida")
    if not MIN_PASSWORD_CHARS <= len(password) <= MAX_PASSWORD_CHARS:
        raise ValueError(
            "La password del backup deve contenere tra "
            f"{MIN_PASSWORD_CHARS} e {MAX_PASSWORD_CHARS} caratteri"
        )
    return password


def is_protected_backup(path: Path) -> bool:
    """Return True only for the Voucher Management protected-container magic."""

    try:
        with Path(path).open("rb") as handle:
            return handle.read(len(PROTECTED_BACKUP_MAGIC)) == PROTECTED_BACKUP_MAGIC
    except OSError:
        return False


def _derive_key(password: str, salt: bytes) -> bytes:
    password = validate_backup_password(password)
    return Scrypt(
        salt=salt,
        length=KEY_BYTES,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
    ).derive(password.encode("utf-8"))


def _header(salt: bytes, nonce: bytes) -> bytes:
    return (
        PROTECTED_BACKUP_MAGIC
        + bytes((PROTECTED_BACKUP_VERSION,))
        + salt
        + nonce
    )


def _parse_header(source: Path) -> tuple[bytes, bytes, bytes, int]:
    try:
        total_size = source.stat().st_size
        with source.open("rb") as handle:
            header = handle.read(HEADER_BYTES)
    except OSError as exc:
        raise ProtectedBackupError(
            "Backup protetto non leggibile"
        ) from exc

    if total_size < HEADER_BYTES + TAG_BYTES + 1:
        raise ProtectedBackupError("Backup protetto incompleto")
    if len(header) != HEADER_BYTES:
        raise ProtectedBackupError("Backup protetto incompleto")
    if not header.startswith(PROTECTED_BACKUP_MAGIC):
        raise ProtectedBackupError("Formato backup protetto non riconosciuto")

    version_offset = len(PROTECTED_BACKUP_MAGIC)
    version = header[version_offset]
    if version != PROTECTED_BACKUP_VERSION:
        raise ProtectedBackupError(
            "Versione del backup protetto non supportata"
        )

    salt_start = version_offset + 1
    salt = header[salt_start:salt_start + SALT_BYTES]
    nonce = header[salt_start + SALT_BYTES:HEADER_BYTES]
    return header, salt, nonce, total_size


def encrypt_backup_file(
    source_zip: Path,
    destination: Path,
    password: str,
) -> None:
    """Encrypt one validated ZIP into the authenticated protected container."""

    source_zip = Path(source_zip)
    destination = Path(destination)
    password = validate_backup_password(password)
    salt = os.urandom(SALT_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    header = _header(salt, nonce)
    key = _derive_key(password, salt)

    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        encryptor = Cipher(
            algorithms.AES(key),
            modes.GCM(nonce),
        ).encryptor()
        encryptor.authenticate_additional_data(header)

        with source_zip.open("rb") as source, destination.open("wb") as target:
            target.write(header)
            while True:
                chunk = source.read(CHUNK_BYTES)
                if not chunk:
                    break
                target.write(encryptor.update(chunk))
            target.write(encryptor.finalize())
            target.write(encryptor.tag)
            target.flush()
            os.fsync(target.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _decrypt_pass(
    source: Path,
    *,
    header: bytes,
    nonce: bytes,
    tag: bytes,
    key: bytes,
    ciphertext_bytes: int,
    target=None,
) -> None:
    """Authenticate one ciphertext pass and optionally emit plaintext.

    A first pass with target=None verifies the complete AES-GCM stream while
    discarding plaintext. Only after that succeeds may a second pass write
    plaintext to a caller-provided temporary file. This keeps memory bounded
    and avoids persisting unauthenticated plaintext.
    """

    decryptor = Cipher(
        algorithms.AES(key),
        modes.GCM(nonce, tag),
    ).decryptor()
    decryptor.authenticate_additional_data(header)

    with source.open("rb") as handle:
        handle.seek(HEADER_BYTES)
        remaining = ciphertext_bytes
        while remaining:
            chunk = handle.read(min(CHUNK_BYTES, remaining))
            if not chunk:
                raise ProtectedBackupError("Backup protetto incompleto")
            remaining -= len(chunk)
            plaintext = decryptor.update(chunk)
            if target is not None and plaintext:
                target.write(plaintext)

        final_plaintext = decryptor.finalize()
        if target is not None and final_plaintext:
            target.write(final_plaintext)


def decrypt_backup_to_file(
    source: Path,
    target,
    password: str,
) -> None:
    """Authenticate fully before writing plaintext to a seekable target."""

    source = Path(source)
    password = validate_backup_password(password)
    header, salt, nonce, total_size = _parse_header(source)
    ciphertext_bytes = total_size - HEADER_BYTES - TAG_BYTES
    key = _derive_key(password, salt)

    try:
        with source.open("rb") as handle:
            handle.seek(total_size - TAG_BYTES)
            tag = handle.read(TAG_BYTES)
        if len(tag) != TAG_BYTES:
            raise ProtectedBackupError("Backup protetto incompleto")

        # Pass 1: authenticate the complete encrypted stream without writing
        # plaintext anywhere. The second pass is reached only for a valid tag.
        _decrypt_pass(
            source,
            header=header,
            nonce=nonce,
            tag=tag,
            key=key,
            ciphertext_bytes=ciphertext_bytes,
        )

        # Pass 2: the ciphertext has been authenticated, so plaintext can now
        # be emitted to the anonymous/temporary target used by restore flows.
        target.seek(0)
        target.truncate(0)
        _decrypt_pass(
            source,
            header=header,
            nonce=nonce,
            tag=tag,
            key=key,
            ciphertext_bytes=ciphertext_bytes,
            target=target,
        )
        target.flush()
        target.seek(0)
    except InvalidTag as exc:
        try:
            target.seek(0)
            target.truncate(0)
        except Exception as cleanup_exc:
            LOGGER.warning(
                "protected_backup_target_cleanup_failed "
                "stage=authentication_failure type=%s",
                type(cleanup_exc).__name__,
            )
        raise ProtectedBackupAuthenticationError(
            "Autenticazione backup protetto non riuscita"
        ) from exc
    except Exception:
        try:
            target.seek(0)
            target.truncate(0)
        except Exception as cleanup_exc:
            LOGGER.warning(
                "protected_backup_target_cleanup_failed "
                "stage=general_failure type=%s",
                type(cleanup_exc).__name__,
            )
        raise


def decrypt_backup_file(
    source: Path,
    destination_zip: Path,
    password: str,
) -> None:
    """Decrypt a protected container to a named ZIP for compatibility callers."""

    destination_zip = Path(destination_zip)
    destination_zip.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination_zip.open("w+b") as target:
            decrypt_backup_to_file(
                source,
                target,
                password,
            )
            target.flush()
            os.fsync(target.fileno())
    except Exception:
        destination_zip.unlink(missing_ok=True)
        raise
