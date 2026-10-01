"""Windows CI probe: backup/restore as a limited shared-install operator.

This script is intentionally tiny and is launched by Test-WindowsSharedInstall.ps1
under a non-administrative local account that belongs only to the application's
operator group.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit(
            "usage: test_shared_restore_operator.py <repo-root> <data-root> <work-root>"
        )

    repo_root = Path(sys.argv[1]).resolve()
    data_root = Path(sys.argv[2]).resolve()
    work_root = Path(sys.argv[3]).resolve()
    sys.path.insert(0, str(repo_root / "src"))

    from voucher_management.backup import BackupService
    from voucher_management.database import Database
    from voucher_management.security.history_key import HistoryKeyStore

    for name in ("config", "data", "Print", "Loghi"):
        (data_root / name).mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)

    secret = "0123456789abcdef0123456789abcdef"
    fingerprint = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]
    settings_path = data_root / "config" / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "structure_name": "ACL Restore Original",
                "history_key_fingerprint": fingerprint,
                "controller_api_root": "",
                "controller_cert_sha256": "",
            }
        ),
        encoding="utf-8",
    )
    (data_root / "data" / "history.jsonl").write_text(
        '{"event":"generate","timestamp":"2026-10-01T00:00:00+00:00"}\n',
        encoding="utf-8",
    )
    HistoryKeyStore(data_root).set(secret)

    database_path = data_root / "data" / "voucher_management.db"
    database = Database(database_path)
    try:
        database.initialize()
        database.create_controller(
            name="ACL Probe",
            api_root="https://controller.invalid",
            created_at="2026-10-01T00:00:00+00:00",
        )
    finally:
        database.close()

    paths = SimpleNamespace(
        user_root=data_root,
        data=data_root / "data",
        database=database_path,
    )
    service = BackupService(paths)
    backup = work_root / "operator-restore.zip"
    service.create(backup)

    changed = json.loads(settings_path.read_text(encoding="utf-8"))
    changed["structure_name"] = "ACL Restore Changed"
    settings_path.write_text(json.dumps(changed), encoding="utf-8")

    rollback = service.restore(backup)

    restored = json.loads(settings_path.read_text(encoding="utf-8"))
    if restored.get("structure_name") != "ACL Restore Original":
        raise RuntimeError("limited operator restore did not restore managed data")
    if rollback.parent != data_root / ".maintenance":
        raise RuntimeError("rollback is outside the operator-writable maintenance root")
    if not rollback.is_dir():
        raise RuntimeError("rollback snapshot was not created")

    rollback_settings = json.loads(
        (rollback / "config" / "settings.json").read_text(encoding="utf-8")
    )
    if rollback_settings.get("structure_name") != "ACL Restore Changed":
        raise RuntimeError("rollback does not preserve the pre-restore live state")

    restored_db = Database(database_path)
    try:
        restored_db.initialize()
        if restored_db.connection.execute(
            "SELECT COUNT(*) FROM controllers WHERE name='ACL Probe'"
        ).fetchone()[0] != 1:
            raise RuntimeError("restored SQLite snapshot is incomplete")
    finally:
        restored_db.close()

    print("Limited operator backup/restore integration test OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
