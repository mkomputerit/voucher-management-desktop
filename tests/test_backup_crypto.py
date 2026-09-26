from pathlib import Path
import io
import tempfile

import pytest

from voucher_management.backup_crypto import (
    HEADER_BYTES,
    MIN_PASSWORD_CHARS,
    ProtectedBackupAuthenticationError,
    decrypt_backup_file,
    decrypt_backup_to_file,
    encrypt_backup_file,
    is_protected_backup,
    validate_backup_password,
)


PASSPHRASE = "synthetic-backup-passphrase"


def test_protected_backup_streaming_roundtrip(tmp_path: Path):
    source = tmp_path / "source.zip"
    encrypted = tmp_path / "backup.vmbk"
    restored = tmp_path / "restored.zip"
    payload = (b"voucher-management-test-" * 70000) + b"tail"
    source.write_bytes(payload)

    encrypt_backup_file(source, encrypted, PASSPHRASE)
    decrypt_backup_file(encrypted, restored, PASSPHRASE)

    assert is_protected_backup(encrypted) is True
    assert restored.read_bytes() == payload
    assert encrypted.read_bytes()[:2] != b"PK"


def test_wrong_password_and_tampering_are_authentication_failures(tmp_path: Path):
    source = tmp_path / "source.zip"
    encrypted = tmp_path / "backup.vmbk"
    wrong_output = tmp_path / "wrong.zip"
    tampered_output = tmp_path / "tampered.zip"
    source.write_bytes(b"PK synthetic sensitive payload")
    encrypt_backup_file(source, encrypted, PASSPHRASE)

    with pytest.raises(ProtectedBackupAuthenticationError):
        decrypt_backup_file(
            encrypted,
            wrong_output,
            "different password value",
        )
    assert not wrong_output.exists()

    payload = bytearray(encrypted.read_bytes())
    payload[HEADER_BYTES + 2] ^= 0x01
    encrypted.write_bytes(payload)

    with pytest.raises(ProtectedBackupAuthenticationError):
        decrypt_backup_file(
            encrypted,
            tampered_output,
            PASSPHRASE,
        )
    assert not tampered_output.exists()


def test_header_tampering_is_authenticated(tmp_path: Path):
    source = tmp_path / "source.zip"
    encrypted = tmp_path / "backup.vmbk"
    restored = tmp_path / "restored.zip"
    source.write_bytes(b"PK protected")
    encrypt_backup_file(source, encrypted, PASSPHRASE)

    payload = bytearray(encrypted.read_bytes())
    # Change one salt byte while leaving magic/version parseable. Header is AAD,
    # so this must be indistinguishable from a wrong password/tampered payload.
    payload[10] ^= 0x01
    encrypted.write_bytes(payload)

    with pytest.raises(ProtectedBackupAuthenticationError):
        decrypt_backup_file(encrypted, restored, PASSPHRASE)


def test_password_policy_does_not_strip_or_normalize():
    exact = " " + ("a" * MIN_PASSWORD_CHARS)
    assert validate_backup_password(exact) == exact

    with pytest.raises(ValueError):
        validate_backup_password("x" * (MIN_PASSWORD_CHARS - 1))


def test_plain_zip_is_not_reported_as_protected(tmp_path: Path):
    plain = tmp_path / "plain.zip"
    plain.write_bytes(b"PK\x03\x04synthetic")

    assert is_protected_backup(plain) is False


def test_protected_backup_can_decrypt_to_anonymous_seekable_file(tmp_path: Path):
    source = tmp_path / "source.zip"
    encrypted = tmp_path / "backup.vmbk"
    payload = b"PK anonymous temporary payload" * 100
    source.write_bytes(payload)
    encrypt_backup_file(source, encrypted, PASSPHRASE)

    with tempfile.TemporaryFile(mode="w+b") as target:
        decrypt_backup_to_file(encrypted, target, PASSPHRASE)
        assert target.read() == payload


def test_failed_stream_authentication_clears_anonymous_target(tmp_path: Path):
    source = tmp_path / "source.zip"
    encrypted = tmp_path / "backup.vmbk"
    source.write_bytes(b"PK protected")
    encrypt_backup_file(source, encrypted, PASSPHRASE)

    with tempfile.TemporaryFile(mode="w+b") as target:
        target.write(b"stale")
        target.seek(0)
        with pytest.raises(ProtectedBackupAuthenticationError):
            decrypt_backup_to_file(
                encrypted,
                target,
                "different password value",
            )
        target.seek(0, 2)
        assert target.tell() == 0


def test_failed_authentication_never_writes_plaintext_to_target(tmp_path: Path):
    source = tmp_path / "source.zip"
    encrypted = tmp_path / "backup.vmbk"
    source.write_bytes(b"PK sensitive plaintext that must stay unauthenticated")
    encrypt_backup_file(source, encrypted, PASSPHRASE)

    class RecordingTarget(io.BytesIO):
        def __init__(self):
            super().__init__(b"stale")
            self.write_calls = 0

        def write(self, data):
            self.write_calls += 1
            return super().write(data)

    target = RecordingTarget()
    with pytest.raises(ProtectedBackupAuthenticationError):
        decrypt_backup_to_file(
            encrypted,
            target,
            "different password value",
        )

    assert target.write_calls == 0
    assert target.getvalue() == b""


def test_cleanup_failure_is_logged_without_masking_authentication_error(
    tmp_path: Path,
    caplog,
):
    source = tmp_path / "source.zip"
    encrypted = tmp_path / "backup.vmbk"
    source.write_bytes(b"PK protected")
    encrypt_backup_file(source, encrypted, PASSPHRASE)

    class FailingCleanupTarget(io.BytesIO):
        def truncate(self, size=None):
            raise RuntimeError("synthetic cleanup failure")

    target = FailingCleanupTarget()
    caplog.set_level(
        "WARNING",
        logger="voucher_management.backup_crypto",
    )

    with pytest.raises(ProtectedBackupAuthenticationError):
        decrypt_backup_to_file(
            encrypted,
            target,
            "different password value",
        )

    assert any(
        "protected_backup_target_cleanup_failed" in record.message
        and "RuntimeError" in record.message
        for record in caplog.records
    )
    assert all(str(tmp_path) not in record.message for record in caplog.records)
