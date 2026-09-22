import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from voucher_management.backup import (
    BACKUP_FORMAT,
    BackupError,
    BackupService,
)
from voucher_management.backup_crypto import PROTECTED_BACKUP_MAGIC
from voucher_management.security.history_key import HistoryKeyStore


class BackupServiceTests(unittest.TestCase):
    """Regression tests for portable, self-contained user-data backups."""

    @staticmethod
    def _backup_passphrase(marker: str = "a") -> str:
        return marker * 24

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name) / "VoucherManagement"
        self.paths = SimpleNamespace(
            user_root=root,
            data=root / "data",
        )
        for name in ("config", "data", "Print", "Loghi"):
            (root / name).mkdir(parents=True, exist_ok=True)

        history_secret = "0123456789abcdef0123456789abcdef"
        history_fingerprint = hashlib.sha256(
            history_secret.encode("utf-8")
        ).hexdigest()[:16]
        (root / "config" / "settings.json").write_text(
            json.dumps(
                {
                    "structure_name": "Test",
                    "history_key_fingerprint": history_fingerprint,
                    "controller_api_root": "https://controller.invalid/proxy/network/integration/v1",
                    "controller_cert_sha256": "a" * 64,
                }
            ),
            encoding="utf-8",
        )
        (root / "data" / "history.jsonl").write_text(
            '{"event":"generate"}\n',
            encoding="utf-8",
        )
        (root / "Print" / "voucher.pdf").write_bytes(b"PDF")
        Image.new("RGB", (8, 8), "white").save(root / "Loghi" / "logo.png", format="PNG")

        self.secret_store = HistoryKeyStore(root)
        self.secret_store.set(history_secret)
        self.service = BackupService(self.paths)

    def tearDown(self):
        self.temp.cleanup()

    def test_roundtrip_restores_complete_application_data(self):
        backup = Path(self.temp.name) / "backup.zip"
        self.service.create(backup)

        (self.paths.user_root / "config" / "settings.json").write_text(
            "changed",
            encoding="utf-8",
        )
        (self.paths.user_root / "Print" / "voucher.pdf").unlink()
        self.secret_store.set("fedcba9876543210fedcba9876543210")

        rollback = self.service.restore(backup)

        settings = json.loads(
            (
                self.paths.user_root / "config" / "settings.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(settings["structure_name"], "Test")
        self.assertTrue(
            (self.paths.user_root / "data" / "history.jsonl").exists()
        )
        self.assertTrue(
            (self.paths.user_root / "Print" / "voucher.pdf").exists()
        )
        self.assertTrue(
            (self.paths.user_root / "Loghi" / "logo.png").exists()
        )
        self.assertEqual(
            HistoryKeyStore(self.paths.user_root).get(),
            "0123456789abcdef0123456789abcdef",
        )
        self.assertTrue(rollback.exists())

        with zipfile.ZipFile(backup, "r") as archive:
            manifest = json.loads(
                archive.read("backup_manifest.json").decode("utf-8")
            )
            self.assertEqual(manifest["format"], BACKUP_FORMAT)
            self.assertIn("data/history_secret.key", archive.namelist())
            self.assertNotIn(
                "security/history_secret.bin", archive.namelist()
            )

    def test_encrypted_backup_roundtrip_restores_complete_data(self):
        backup = Path(self.temp.name) / "backup.vmbk"
        passphrase = self._backup_passphrase()

        self.service.create(backup, password=passphrase)

        self.assertTrue(self.service.is_encrypted_backup(backup))
        self.assertTrue(backup.read_bytes().startswith(PROTECTED_BACKUP_MAGIC))
        self.assertNotIn(
            b"0123456789abcdef0123456789abcdef",
            backup.read_bytes(),
        )
        manifest = self.service.validate_encrypted(backup, passphrase)
        self.assertEqual(manifest["format"], BACKUP_FORMAT)

        (self.paths.user_root / "config" / "settings.json").write_text(
            "changed",
            encoding="utf-8",
        )
        (self.paths.user_root / "Print" / "voucher.pdf").unlink()
        self.secret_store.set("fedcba9876543210fedcba9876543210")

        rollback = self.service.restore(backup, password=passphrase)

        settings = json.loads(
            (self.paths.user_root / "config" / "settings.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(settings["structure_name"], "Test")
        self.assertTrue(
            (self.paths.user_root / "Print" / "voucher.pdf").exists()
        )
        self.assertEqual(
            HistoryKeyStore(self.paths.user_root).get(),
            "0123456789abcdef0123456789abcdef",
        )
        self.assertTrue(rollback.exists())

    def test_encrypted_backup_creation_does_not_build_plaintext_zip(self):
        backup = Path(self.temp.name) / "backup.vmbk"

        with patch.object(
            self.service,
            "_create_zip",
            side_effect=AssertionError(
                "encrypted backup must stream directly into encryption"
            ),
        ):
            self.service.create(
                backup,
                password=self._backup_passphrase(),
            )

        self.assertTrue(backup.exists())
        self.assertTrue(self.service.is_encrypted_backup(backup))

    def test_encrypted_backup_rejects_destination_inside_live_data_root(self):
        backup = self.paths.user_root / "data" / "backup.vmbk"

        with self.assertRaisesRegex(
            BackupError,
            "fuori dalla cartella dati",
        ):
            self.service.create(
                backup,
                password=self._backup_passphrase(),
            )

        self.assertFalse(backup.exists())

    def test_encrypted_backup_wrong_password_does_not_touch_live_data(self):
        backup = Path(self.temp.name) / "backup.vmbk"
        self.service.create(
            backup,
            password=self._backup_passphrase(),
        )
        settings_path = self.paths.user_root / "config" / "settings.json"
        before = settings_path.read_bytes()

        with self.assertRaisesRegex(
            BackupError,
            "Password non valida oppure backup cifrato alterato",
        ):
            self.service.restore(
                backup,
                password=self._backup_passphrase("b"),
            )

        self.assertEqual(settings_path.read_bytes(), before)
        self.assertEqual(
            list(self.paths.user_root.parent.glob("VoucherManagement-rollback-*")),
            [],
        )

    def test_encrypted_backup_tampering_is_detected_before_restore(self):
        backup = Path(self.temp.name) / "backup.vmbk"
        passphrase = self._backup_passphrase()
        self.service.create(backup, password=passphrase)

        payload = bytearray(backup.read_bytes())
        self.assertGreater(len(payload), len(PROTECTED_BACKUP_MAGIC) + 40)
        payload[len(payload) // 2] ^= 0x01
        backup.write_bytes(payload)

        with self.assertRaisesRegex(
            BackupError,
            "Password non valida oppure backup cifrato alterato",
        ):
            self.service.validate_encrypted(backup, passphrase)

    def test_encrypted_backup_requires_real_password_not_empty_string(self):
        backup = Path(self.temp.name) / "backup.vmbk"

        with self.assertRaisesRegex(
            BackupError,
            "tra 12 e 1024 caratteri",
        ):
            self.service.create(backup, password="")

        self.assertFalse(backup.exists())

    def test_legacy_zip_remains_readable_without_password(self):
        backup = Path(self.temp.name) / "legacy.zip"

        self.service.create(backup)

        self.assertFalse(self.service.is_encrypted_backup(backup))
        manifest = self.service.validate(backup)
        self.assertEqual(manifest["format"], BACKUP_FORMAT)

    def test_backup_excludes_renderer_temp_files(self):
        temp_dir = self.paths.user_root / "Print" / "2026" / "09"
        temp_dir.mkdir(parents=True, exist_ok=True)
        orphan = temp_dir / ".Voucher_Test_20260921_120000-deadbeef.tmp"
        orphan.write_bytes(b"%PDF-sensitive-voucher-code")
        managed_pdf = temp_dir / "Voucher_Test_20260921_120000.pdf"
        managed_pdf.write_bytes(b"%PDF")

        backup = Path(self.temp.name) / "backup.zip"
        self.service.create(backup)

        with zipfile.ZipFile(backup, "r") as archive:
            names = set(archive.namelist())

        self.assertIn(
            "Print/2026/09/Voucher_Test_20260921_120000.pdf",
            names,
        )
        self.assertNotIn(
            "Print/2026/09/.Voucher_Test_20260921_120000-deadbeef.tmp",
            names,
        )


    def test_backup_is_blocked_while_print_audit_is_pending(self):
        pending = self.paths.user_root / "data" / "pending_print_audit.json"
        pending.write_text(
            '{"format":1,"audit_id":"synthetic"}\n',
            encoding="utf-8",
        )
        backup = Path(self.temp.name) / "backup.zip"

        with self.assertRaisesRegex(
            BackupError,
            "stampa fisica ancora da registrare",
        ):
            self.service.create(backup)

        self.assertFalse(backup.exists())

    def test_backup_is_blocked_while_create_outcome_is_unresolved(self):
        pending = self.paths.user_root / "data" / "pending_create_guard"
        pending.write_text("pending\n", encoding="ascii")
        backup = Path(self.temp.name) / "backup.zip"

        with self.assertRaisesRegex(
            BackupError,
            "creazione voucher con esito ancora da verificare",
        ):
            self.service.create(backup)

        self.assertFalse(backup.exists())

    def test_restore_is_blocked_while_create_outcome_is_unresolved(self):
        backup = Path(self.temp.name) / "backup.zip"
        self.service.create(backup)
        pending = self.paths.user_root / "data" / "pending_create_guard"
        pending.write_text("pending\n", encoding="ascii")

        with self.assertRaisesRegex(
            BackupError,
            "creazione voucher con esito ancora da verificare",
        ):
            self.service.restore(backup)


    def test_encrypted_validation_does_not_use_named_decrypted_zip(self):
        backup = Path(self.temp.name) / "backup.vmbk"
        passphrase = self._backup_passphrase()
        self.service.create(backup, password=passphrase)

        with patch.object(
            self.service,
            "_decrypt_to_zip",
            side_effect=AssertionError(
                "named plaintext ZIP path must not be used"
            ),
        ):
            manifest = self.service.validate_encrypted(
                backup,
                passphrase,
            )

        self.assertEqual(manifest["format"], BACKUP_FORMAT)

    def test_encrypted_restore_does_not_use_named_decrypted_zip(self):
        backup = Path(self.temp.name) / "backup.vmbk"
        passphrase = self._backup_passphrase()
        self.service.create(backup, password=passphrase)

        with patch.object(
            self.service,
            "_decrypt_to_zip",
            side_effect=AssertionError(
                "named plaintext ZIP path must not be used"
            ),
        ):
            rollback = self.service.restore(
                backup,
                password=passphrase,
            )

        self.assertTrue(rollback.exists())
        self.assertEqual(
            list(
                self.paths.user_root.parent.glob(
                    "voucher-management-decrypted-*.zip"
                )
            ),
            [],
        )

    def test_create_preserves_previous_backup_until_new_archive_validates(self):
        destination = Path(self.temp.name) / "backup.zip"
        previous = b"previous-known-good-backup"
        destination.write_bytes(previous)

        with patch.object(
            self.service,
            "validate",
            side_effect=BackupError("synthetic validation failure"),
        ):
            with self.assertRaises(BackupError):
                self.service.create(destination)

        self.assertEqual(destination.read_bytes(), previous)

    def test_failed_rollback_copy_keeps_live_data_untouched(self):
        backup = Path(self.temp.name) / "backup.zip"
        self.service.create(backup)

        settings_path = self.paths.user_root / "config" / "settings.json"
        history_path = self.paths.user_root / "data" / "history.jsonl"
        key_path = self.paths.user_root / "data" / "history_secret.key"
        before = {
            "settings": settings_path.read_bytes(),
            "history": history_path.read_bytes(),
            "key": key_path.read_bytes(),
        }

        import shutil
        real_copytree = shutil.copytree

        def failing_copytree(src, dst, *args, **kwargs):
            if Path(src) == self.paths.user_root:
                Path(dst).mkdir(parents=True, exist_ok=True)
                (Path(dst) / "partial.txt").write_text(
                    "partial",
                    encoding="utf-8",
                )
                raise OSError("synthetic disk full")
            return real_copytree(src, dst, *args, **kwargs)

        with patch(
            "voucher_management.backup.shutil.copytree",
            side_effect=failing_copytree,
        ):
            with self.assertRaises(BackupError):
                self.service.restore(backup)

        self.assertEqual(settings_path.read_bytes(), before["settings"])
        self.assertEqual(history_path.read_bytes(), before["history"])
        self.assertEqual(key_path.read_bytes(), before["key"])


    def test_failed_restore_recovers_live_data_and_removes_sensitive_rollback(self):
        backup = Path(self.temp.name) / "backup.zip"
        self.service.create(backup)

        settings_path = self.paths.user_root / "config" / "settings.json"
        before = settings_path.read_bytes()

        import shutil
        real_copytree = shutil.copytree
        injected = {"done": False}

        def failing_once(src, dst, *args, **kwargs):
            src_path = Path(src)
            if (
                not injected["done"]
                and src_path.name == "config"
                and "voucher-management-restore-" in str(src_path.parent)
            ):
                injected["done"] = True
                raise OSError("synthetic restore copy failure")
            return real_copytree(src, dst, *args, **kwargs)

        with patch(
            "voucher_management.backup.shutil.copytree",
            side_effect=failing_once,
        ):
            with self.assertRaises(BackupError):
                self.service.restore(backup)

        self.assertEqual(settings_path.read_bytes(), before)
        rollbacks = list(
            self.paths.user_root.parent.glob("VoucherManagement-rollback-*")
        )
        self.assertEqual(rollbacks, [])

    def test_rejects_zip_without_manifest(self):
        bad = Path(self.temp.name) / "bad.zip"
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("config/settings.json", "{}")
        with self.assertRaises(BackupError):
            self.service.validate(bad)

    def test_rejects_path_traversal(self):
        bad = Path(self.temp.name) / "traversal.zip"
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr(
                "backup_manifest.json",
                json.dumps({"format": BACKUP_FORMAT}),
            )
            archive.writestr("../outside.txt", "no")
        with self.assertRaises(BackupError):
            self.service.validate(bad)

    def test_rejects_backup_inside_live_data_root(self):
        target = self.paths.user_root / "data" / "backup.zip"
        with self.assertRaises(BackupError):
            self.service.create(target)

    def test_format_one_portable_key_is_migrated(self):
        old = Path(self.temp.name) / "old-format.zip"
        legacy_store = HistoryKeyStore(
            Path(self.temp.name) / "legacy-source"
        )
        legacy_store.set("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        legacy_payload = legacy_store.path.read_bytes()

        with zipfile.ZipFile(old, "w") as archive:
            archive.writestr(
                "backup_manifest.json",
                json.dumps({"format": 1}),
            )
            archive.writestr(
                "config/settings.json",
                json.dumps({"structure_name": "Old"}),
            )
            archive.writestr(
                "security/history_secret.bin",
                legacy_payload,
            )

        self.service.restore(old)
        self.assertEqual(
            HistoryKeyStore(self.paths.user_root).get(),
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )


    def test_rejects_case_colliding_archive_paths(self):
        bad = Path(self.temp.name) / "collision.zip"
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr(
                "backup_manifest.json",
                json.dumps({"format": BACKUP_FORMAT}),
            )
            archive.writestr("config/settings.json", "{}")
            archive.writestr("CONFIG/settings.json", "{}")

        with self.assertRaises(BackupError):
            self.service.validate(bad)


    def test_rejects_windows_alternate_data_stream_name(self):
        bad = Path(self.temp.name) / "ads.zip"
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr(
                "backup_manifest.json",
                json.dumps({"format": BACKUP_FORMAT}),
            )
            archive.writestr("config/settings.json:payload", "{}")

        with self.assertRaises(BackupError):
            self.service.validate(bad)

    def test_rejects_windows_reserved_device_name(self):
        bad = Path(self.temp.name) / "device.zip"
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr(
                "backup_manifest.json",
                json.dumps({"format": BACKUP_FORMAT}),
            )
            archive.writestr("config/NUL.txt", "no")

        with self.assertRaises(BackupError):
            self.service.validate(bad)


    def test_backup_sanitizes_settings_and_legacy_absolute_paths(self):
        settings_path = self.paths.user_root / "config" / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "wifi_title": "Guest",
                    "logo_path": r"C:\\LegacyProfile\\Example\\logo.png",
                    "password": "must-not-be-exported",
                }
            ),
            encoding="utf-8",
        )
        history_path = self.paths.user_root / "data" / "history.jsonl"
        history_path.write_text(
            json.dumps(
                {
                    "event": "generate",
                    "voucher_id": "digest",
                    "output_file": r"C:\\LegacyProfile\\Example\\Print\\Voucher_Test.pdf",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        backup = Path(self.temp.name) / "sanitized.zip"
        self.service.create(backup)

        with zipfile.ZipFile(backup, "r") as archive:
            settings = json.loads(
                archive.read("config/settings.json").decode("utf-8")
            )
            history_line = json.loads(
                archive.read("data/history.jsonl")
                .decode("utf-8")
                .strip()
            )

        self.assertNotIn("password", settings)
        self.assertEqual(settings["logo_path"], "logo.png")
        self.assertEqual(
            history_line["output_file"],
            "Voucher_Test.pdf",
        )


    def test_rejects_format_two_backup_for_another_application(self):
        bad = Path(self.temp.name) / "other-app.zip"
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr(
                "backup_manifest.json",
                json.dumps(
                    {
                        "format": BACKUP_FORMAT,
                        "application": "Another Application",
                    }
                ),
            )
            archive.writestr("config/settings.json", "{}")

        with self.assertRaises(BackupError):
            self.service.validate(bad)


    def test_restore_sanitizes_out_of_range_numeric_settings(self):
        backup = Path(self.temp.name) / "backup.zip"
        self.service.create(backup)

        tampered = Path(self.temp.name) / "tampered-settings.zip"
        with zipfile.ZipFile(backup, "r") as src, zipfile.ZipFile(
            tampered, "w", compression=zipfile.ZIP_DEFLATED
        ) as dst:
            for info in src.infolist():
                payload = src.read(info.filename)
                if info.filename == "config/settings.json":
                    settings = json.loads(payload.decode("utf-8"))
                    settings["print_retention_days"] = True
                    settings["log_retention_days"] = "abc"
                    payload = json.dumps(settings).encode("utf-8")
                dst.writestr(info, payload)

        self.service.restore(tampered)

        restored = json.loads(
            (
                self.paths.user_root / "config" / "settings.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(restored["print_retention_days"], 0)
        self.assertEqual(restored["log_retention_days"], 30)


    def test_restore_clears_controller_target_and_pin(self):
        backup = Path(self.temp.name) / "backup.zip"
        self.service.create(backup)

        self.service.restore(backup)

        settings = json.loads(
            (self.paths.user_root / "config" / "settings.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(settings["controller_api_root"], "")
        self.assertEqual(settings["controller_cert_sha256"], "")

    def test_restore_rejects_history_key_fingerprint_mismatch_before_live_change(self):
        backup = Path(self.temp.name) / "backup.zip"
        self.service.create(backup)
        live_settings = (
            self.paths.user_root / "config" / "settings.json"
        ).read_bytes()

        tampered = Path(self.temp.name) / "tampered.zip"
        with zipfile.ZipFile(backup, "r") as src, zipfile.ZipFile(
            tampered, "w", compression=zipfile.ZIP_DEFLATED
        ) as dst:
            for info in src.infolist():
                payload = src.read(info.filename)
                if info.filename == "config/settings.json":
                    settings = json.loads(payload.decode("utf-8"))
                    settings["history_key_fingerprint"] = "deadbeefdeadbeef"
                    payload = json.dumps(settings).encode("utf-8")
                dst.writestr(info, payload)

        with self.assertRaisesRegex(BackupError, "incoerente"):
            self.service.restore(tampered)

        self.assertEqual(
            (self.paths.user_root / "config" / "settings.json").read_bytes(),
            live_settings,
        )

    def test_restore_rejects_disguised_non_png_logo_before_live_change(self):
        backup = Path(self.temp.name) / "backup.zip"
        self.service.create(backup)
        live_settings = (
            self.paths.user_root / "config" / "settings.json"
        ).read_bytes()

        tampered = Path(self.temp.name) / "tampered-logo.zip"
        with zipfile.ZipFile(backup, "r") as src, zipfile.ZipFile(
            tampered, "w", compression=zipfile.ZIP_DEFLATED
        ) as dst:
            for info in src.infolist():
                payload = src.read(info.filename)
                if info.filename == "Loghi/logo.png":
                    payload = b"8BPS\x00\x01synthetic-psd-payload"
                dst.writestr(info, payload)

        with self.assertRaisesRegex(BackupError, "logo non valido"):
            self.service.restore(tampered)

        self.assertEqual(
            (self.paths.user_root / "config" / "settings.json").read_bytes(),
            live_settings,
        )


    def test_restore_quarantines_oversized_legacy_logo(self):
        settings_path = self.paths.user_root / "config" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        settings["logo_path"] = str(
            self.paths.user_root / "Loghi" / "logo.png"
        )
        settings_path.write_text(json.dumps(settings), encoding="utf-8")

        backup = Path(self.temp.name) / "backup.zip"
        self.service.create(backup)

        oversized = Path(self.temp.name) / "oversized.png"
        Image.new("1", (7000, 6000), 1).save(oversized, format="PNG")
        oversized_payload = oversized.read_bytes()

        tampered = Path(self.temp.name) / "oversized-logo.zip"
        with zipfile.ZipFile(backup, "r") as src, zipfile.ZipFile(
            tampered, "w", compression=zipfile.ZIP_DEFLATED
        ) as dst:
            for info in src.infolist():
                payload = src.read(info.filename)
                if info.filename == "Loghi/logo.png":
                    payload = oversized_payload
                dst.writestr(info, payload)

        self.service.restore(tampered)
        warnings = self.service.consume_restore_warnings()

        restored_settings = json.loads(
            settings_path.read_text(encoding="utf-8")
        )
        self.assertEqual(restored_settings["logo_path"], "")
        self.assertFalse(
            (self.paths.user_root / "Loghi" / "logo.png").exists()
        )
        self.assertTrue(
            (self.paths.user_root / "data" / "history.jsonl").exists()
        )
        self.assertEqual(
            HistoryKeyStore(self.paths.user_root).get(),
            "0123456789abcdef0123456789abcdef",
        )
        self.assertEqual(len(warnings), 1)
        self.assertIn("logo.png", warnings[0])
        self.assertIn("40 megapixel", warnings[0])


    def test_rejects_oversized_manifest_without_reading_it(self):
        bad = Path(self.temp.name) / "large-manifest.zip"
        with zipfile.ZipFile(bad, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(
                "backup_manifest.json",
                b"{" + b" " * (70 * 1024) + b"}",
            )

        with self.assertRaisesRegex(BackupError, "Manifest backup troppo grande"):
            self.service.validate(bad)


    def test_backup_excludes_corrupt_settings_forensic_copies(self):
        corrupt = (
            self.paths.user_root
            / "config"
            / "settings.json.corrupt-synthetic"
        )
        corrupt.write_text("{unexpected archival payload", encoding="utf-8")
        backup = Path(self.temp.name) / "backup.zip"

        self.service.create(backup)

        with zipfile.ZipFile(backup, "r") as archive:
            self.assertNotIn(
                "config/settings.json.corrupt-synthetic",
                archive.namelist(),
            )

    def test_restore_rejects_malformed_history_before_live_change(self):
        backup = Path(self.temp.name) / "backup.zip"
        self.service.create(backup)
        live_settings = (
            self.paths.user_root / "config" / "settings.json"
        ).read_bytes()

        tampered = Path(self.temp.name) / "tampered-history.zip"
        with zipfile.ZipFile(backup, "r") as src, zipfile.ZipFile(
            tampered, "w", compression=zipfile.ZIP_DEFLATED
        ) as dst:
            for info in src.infolist():
                payload = src.read(info.filename)
                if info.filename == "data/history.jsonl":
                    payload = b"{not-json}\n"
                dst.writestr(info, payload)

        with self.assertRaisesRegex(BackupError, "cronologia danneggiata"):
            self.service.restore(tampered)

        self.assertEqual(
            (self.paths.user_root / "config" / "settings.json").read_bytes(),
            live_settings,
        )


if __name__ == "__main__":
    unittest.main()
