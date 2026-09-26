"""SQLite persistence foundation for Voucher Management 5.0.

The database is the durable local history of controller observations and
operator actions.  UniFi remains authoritative for voucher use/expiry, while
Voucher Management remains authoritative for PDF/physical-print audit data.

No controller API credential is accepted by this module or represented in the
schema.  All timestamps are stored as ISO-8601 UTC text supplied by callers.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 2


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS app_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS controllers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    api_root TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    cert_sha256 TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    last_successful_sync_at TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS application_sessions (
    id INTEGER PRIMARY KEY,
    session_uuid TEXT NOT NULL UNIQUE,
    windows_user TEXT NOT NULL,
    started_at TEXT NOT NULL,
    closed_at TEXT,
    controller_id INTEGER REFERENCES controllers(id),
    close_status TEXT,
    backup_status TEXT,
    app_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS installation_profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    installation_name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    logo_filename TEXT NOT NULL DEFAULT '',
    pdf_title TEXT NOT NULL DEFAULT '',
    pdf_subtitle TEXT NOT NULL DEFAULT '',
    pdf_contact TEXT NOT NULL DEFAULT '',
    pdf_notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vouchers (
    id INTEGER PRIMARY KEY,
    controller_id INTEGER NOT NULL REFERENCES controllers(id),
    unifi_id TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    created_at TEXT,
    imported_at TEXT NOT NULL,
    duration_minutes INTEGER CHECK (duration_minutes IS NULL OR duration_minutes >= 0),
    authorized_guest_limit INTEGER CHECK (authorized_guest_limit IS NULL OR authorized_guest_limit >= 1),
    authorized_guest_count INTEGER NOT NULL DEFAULT 0 CHECK (authorized_guest_count >= 0),
    activated_at TEXT,
    expires_at TEXT,
    expired INTEGER NOT NULL DEFAULT 0 CHECK (expired IN (0, 1)),
    data_limit_mb INTEGER CHECK (data_limit_mb IS NULL OR data_limit_mb >= 0),
    download_limit_kbps INTEGER CHECK (download_limit_kbps IS NULL OR download_limit_kbps >= 0),
    upload_limit_kbps INTEGER CHECK (upload_limit_kbps IS NULL OR upload_limit_kbps >= 0),
    present_on_controller INTEGER NOT NULL DEFAULT 1 CHECK (present_on_controller IN (0, 1)),
    last_seen_at TEXT,
    last_synced_at TEXT NOT NULL,
    assigned_to TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    archived_at TEXT,
    UNIQUE (controller_id, unifi_id)
);

CREATE INDEX IF NOT EXISTS idx_vouchers_controller ON vouchers(controller_id);
CREATE INDEX IF NOT EXISTS idx_vouchers_code ON vouchers(controller_id, code);
CREATE INDEX IF NOT EXISTS idx_vouchers_expired ON vouchers(controller_id, expired);
CREATE INDEX IF NOT EXISTS idx_vouchers_usage ON vouchers(controller_id, authorized_guest_count);
CREATE INDEX IF NOT EXISTS idx_vouchers_expires ON vouchers(expires_at);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY,
    sync_uuid TEXT NOT NULL UNIQUE,
    controller_id INTEGER NOT NULL REFERENCES controllers(id),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    vouchers_received INTEGER,
    changes_detected INTEGER,
    error_summary TEXT
);

CREATE TABLE IF NOT EXISTS voucher_sync_observations (
    id INTEGER PRIMARY KEY,
    voucher_id INTEGER NOT NULL REFERENCES vouchers(id),
    observed_at TEXT NOT NULL,
    field_name TEXT NOT NULL,
    previous_value TEXT,
    new_value TEXT,
    sync_uuid TEXT NOT NULL REFERENCES sync_runs(sync_uuid)
);

CREATE INDEX IF NOT EXISTS idx_observations_voucher
ON voucher_sync_observations(voucher_id, observed_at);

CREATE TABLE IF NOT EXISTS voucher_events (
    id INTEGER PRIMARY KEY,
    event_uuid TEXT NOT NULL UNIQUE,
    voucher_id INTEGER NOT NULL REFERENCES vouchers(id),
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('UNIFI', 'APPLICATION', 'OPERATOR', 'SYSTEM', 'MIGRATION')),
    windows_user TEXT,
    details_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_voucher
ON voucher_events(voucher_id, occurred_at);

CREATE TABLE IF NOT EXISTS print_jobs (
    id INTEGER PRIMARY KEY,
    print_job_uuid TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    submitted_at TEXT,
    windows_user TEXT NOT NULL,
    output_file TEXT,
    document_copies INTEGER NOT NULL DEFAULT 1 CHECK (document_copies >= 1),
    status TEXT NOT NULL CHECK (status IN ('PREPARED', 'SUBMITTED', 'AUDITED', 'CANCELLED', 'UNCERTAIN'))
);

CREATE TABLE IF NOT EXISTS voucher_prints (
    id INTEGER PRIMARY KEY,
    print_job_id INTEGER NOT NULL REFERENCES print_jobs(id),
    voucher_id INTEGER NOT NULL REFERENCES vouchers(id),
    printed_at TEXT NOT NULL,
    windows_user TEXT NOT NULL,
    physical_copies INTEGER NOT NULL CHECK (physical_copies >= 1),
    print_sequence INTEGER NOT NULL CHECK (print_sequence >= 1),
    is_reprint INTEGER NOT NULL CHECK (is_reprint IN (0, 1)),
    UNIQUE (voucher_id, print_sequence)
);

CREATE INDEX IF NOT EXISTS idx_voucher_prints_voucher
ON voucher_prints(voucher_id, printed_at);

CREATE TABLE IF NOT EXISTS retention_policy (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    unused_unprinted_days INTEGER NOT NULL DEFAULT 180 CHECK (unused_unprinted_days >= 1),
    protect_used INTEGER NOT NULL DEFAULT 1 CHECK (protect_used = 1),
    protect_printed INTEGER NOT NULL DEFAULT 1 CHECK (protect_printed = 1),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backup_history (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    destination TEXT NOT NULL,
    filename TEXT,
    status TEXT NOT NULL,
    sha256 TEXT,
    backup_format INTEGER,
    schema_version INTEGER,
    error_summary TEXT
);

CREATE TABLE IF NOT EXISTS migration_runs (
    id INTEGER PRIMARY KEY,
    migration_uuid TEXT NOT NULL UNIQUE,
    source_kind TEXT NOT NULL CHECK (source_kind = 'LEGACY_4X_HISTORY'),
    source_history_sha256 TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('STARTED', 'EVIDENCE_READY', 'COMPLETED', 'FAILED')
    ),
    total_rows INTEGER NOT NULL DEFAULT 0 CHECK (total_rows >= 0),
    resolved_rows INTEGER NOT NULL DEFAULT 0 CHECK (resolved_rows >= 0),
    ambiguous_rows INTEGER NOT NULL DEFAULT 0 CHECK (ambiguous_rows >= 0),
    unresolved_rows INTEGER NOT NULL DEFAULT 0 CHECK (unresolved_rows >= 0),
    error_summary TEXT
);

CREATE TABLE IF NOT EXISTS legacy_audit_events (
    legacy_event_key TEXT PRIMARY KEY,
    source_line INTEGER NOT NULL CHECK (source_line >= 1),
    voucher_digest TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('generate', 'print')),
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    resolution_status TEXT NOT NULL CHECK (
        resolution_status IN ('RESOLVED', 'AMBIGUOUS', 'UNRESOLVED')
    ),
    voucher_id INTEGER REFERENCES vouchers(id),
    first_migration_uuid TEXT NOT NULL REFERENCES migration_runs(migration_uuid),
    last_migration_uuid TEXT NOT NULL REFERENCES migration_runs(migration_uuid),
    materialized_at TEXT,
    CHECK (
        (resolution_status = 'RESOLVED' AND voucher_id IS NOT NULL)
        OR
        (resolution_status IN ('AMBIGUOUS', 'UNRESOLVED') AND voucher_id IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_legacy_audit_voucher
ON legacy_audit_events(voucher_id, occurred_at);

CREATE INDEX IF NOT EXISTS idx_legacy_audit_resolution
ON legacy_audit_events(resolution_status, occurred_at);
"""


MIGRATION_1_TO_2_SQL = """
CREATE TABLE IF NOT EXISTS migration_runs (
    id INTEGER PRIMARY KEY,
    migration_uuid TEXT NOT NULL UNIQUE,
    source_kind TEXT NOT NULL CHECK (source_kind = 'LEGACY_4X_HISTORY'),
    source_history_sha256 TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('STARTED', 'EVIDENCE_READY', 'COMPLETED', 'FAILED')
    ),
    total_rows INTEGER NOT NULL DEFAULT 0 CHECK (total_rows >= 0),
    resolved_rows INTEGER NOT NULL DEFAULT 0 CHECK (resolved_rows >= 0),
    ambiguous_rows INTEGER NOT NULL DEFAULT 0 CHECK (ambiguous_rows >= 0),
    unresolved_rows INTEGER NOT NULL DEFAULT 0 CHECK (unresolved_rows >= 0),
    error_summary TEXT
);

CREATE TABLE IF NOT EXISTS legacy_audit_events (
    legacy_event_key TEXT PRIMARY KEY,
    source_line INTEGER NOT NULL CHECK (source_line >= 1),
    voucher_digest TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('generate', 'print')),
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    resolution_status TEXT NOT NULL CHECK (
        resolution_status IN ('RESOLVED', 'AMBIGUOUS', 'UNRESOLVED')
    ),
    voucher_id INTEGER REFERENCES vouchers(id),
    first_migration_uuid TEXT NOT NULL REFERENCES migration_runs(migration_uuid),
    last_migration_uuid TEXT NOT NULL REFERENCES migration_runs(migration_uuid),
    materialized_at TEXT,
    CHECK (
        (resolution_status = 'RESOLVED' AND voucher_id IS NOT NULL)
        OR
        (resolution_status IN ('AMBIGUOUS', 'UNRESOLVED') AND voucher_id IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_legacy_audit_voucher
ON legacy_audit_events(voucher_id, occurred_at);

CREATE INDEX IF NOT EXISTS idx_legacy_audit_resolution
ON legacy_audit_events(resolution_status, occurred_at);
"""


@dataclass(frozen=True)
class PrintAuditSummary:
    """Aggregated local print facts used by the duplicate-print warning."""

    print_jobs: int
    physical_copies: int
    first_printed_at: str
    last_printed_at: str


class Database:
    """Own one SQLite connection and the versioned 5.0 schema.

    WAL improves resilience for the shared ProgramData database, while
    busy_timeout avoids treating a short-lived writer as database corruption.
    A machine-wide single-instance guard will still be required by the Windows
    shell before this database is opened for normal application use.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 5000")

    def close(self) -> None:
        """Close the underlying SQLite connection."""

        self.connection.close()

    def initialize(self) -> None:
        """Create/upgrade the current schema and reject newer databases."""

        current = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema {current} is newer than supported {SCHEMA_VERSION}"
            )
        if current == 0:
            with self.transaction():
                self.connection.executescript(SCHEMA_SQL)
                self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self.connection.execute(
                    "INSERT OR REPLACE INTO app_metadata(key, value) VALUES (?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
                )
        elif current == 1:
            try:
                # sqlite3.executescript() controls transaction boundaries on
                # its own. Put BEGIN/COMMIT inside the script so an interrupted
                # schema upgrade cannot leave only part of the v2 evidence
                # schema installed.
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + MIGRATION_1_TO_2_SQL
                    + """
PRAGMA user_version = 2;
INSERT OR REPLACE INTO app_metadata(key, value)
VALUES ('schema_version', '2');
COMMIT;
"""
                )
            except Exception:
                self.connection.rollback()
                raise
        elif current < SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema migration {current}->{SCHEMA_VERSION} is not implemented"
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Commit all writes together or roll them back on any exception."""

        try:
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def integrity_check(self) -> None:
        """Raise when SQLite reports anything other than a healthy database."""

        result = self.connection.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {result}")

    def create_controller(
        self, *, name: str, api_root: str, created_at: str,
        description: str = "", cert_sha256: str = "",
    ) -> int:
        """Persist non-secret controller identity; credentials are never accepted."""

        with self.transaction() as db:
            cursor = db.execute(
                """INSERT INTO controllers
                   (name, api_root, description, cert_sha256, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (name.strip(), api_root.strip(), description.strip(), cert_sha256.strip(), created_at),
            )
            return int(cursor.lastrowid)

    def find_controller_by_api_root(self, api_root: str) -> int | None:
        """Return the active controller matching an API root, if already known."""

        row = self.connection.execute(
            """SELECT id FROM controllers
               WHERE api_root=? AND is_active=1 ORDER BY id LIMIT 1""",
            (api_root.strip(),),
        ).fetchone()
        return None if row is None else int(row["id"])

    def get_or_create_controller(
        self, *, name: str, api_root: str, observed_at: str,
        cert_sha256: str = "",
    ) -> int:
        """Resolve one non-secret controller profile by normalized API root."""

        row = self.connection.execute(
            "SELECT id FROM controllers WHERE api_root=? AND is_active=1 ORDER BY id LIMIT 1",
            (api_root.strip(),),
        ).fetchone()
        if row is not None:
            with self.transaction() as db:
                db.execute(
                    """UPDATE controllers SET name=?, cert_sha256=?, last_used_at=?
                       WHERE id=?""",
                    (name.strip(), cert_sha256.strip(), observed_at, row["id"]),
                )
            return int(row["id"])
        return self.create_controller(
            name=name,
            api_root=api_root,
            created_at=observed_at,
            cert_sha256=cert_sha256,
        )

    def upsert_voucher(
        self, *, controller_id: int, unifi_id: str, code: str, imported_at: str,
        last_synced_at: str, name: str = "", created_at: str | None = None,
        duration_minutes: int | None = None, authorized_guest_limit: int | None = None,
        authorized_guest_count: int = 0, activated_at: str | None = None,
        expires_at: str | None = None, expired: bool = False,
        data_limit_mb: int | None = None, download_limit_kbps: int | None = None,
        upload_limit_kbps: int | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        """Insert/update latest UniFi state without destroying local history.

        Supplying connection lets a higher-level workflow include the upsert in
        its own transaction; otherwise this method owns a short transaction.
        """

        values = (
            controller_id, unifi_id, code, name, created_at, imported_at,
            duration_minutes, authorized_guest_limit, authorized_guest_count,
            activated_at, expires_at, int(expired), data_limit_mb,
            download_limit_kbps, upload_limit_kbps, last_synced_at, last_synced_at,
        )
        def write(db: sqlite3.Connection) -> int:
            db.execute(
                """INSERT INTO vouchers (
                       controller_id, unifi_id, code, name, created_at, imported_at,
                       duration_minutes, authorized_guest_limit, authorized_guest_count,
                       activated_at, expires_at, expired, data_limit_mb,
                       download_limit_kbps, upload_limit_kbps, last_seen_at, last_synced_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(controller_id, unifi_id) DO UPDATE SET
                       code=excluded.code, name=excluded.name, created_at=excluded.created_at,
                       duration_minutes=excluded.duration_minutes,
                       authorized_guest_limit=excluded.authorized_guest_limit,
                       authorized_guest_count=excluded.authorized_guest_count,
                       activated_at=excluded.activated_at, expires_at=excluded.expires_at,
                       expired=excluded.expired, data_limit_mb=excluded.data_limit_mb,
                       download_limit_kbps=excluded.download_limit_kbps,
                       upload_limit_kbps=excluded.upload_limit_kbps,
                       present_on_controller=1, last_seen_at=excluded.last_seen_at,
                       last_synced_at=excluded.last_synced_at""",
                values,
            )
            row = db.execute(
                "SELECT id FROM vouchers WHERE controller_id=? AND unifi_id=?",
                (controller_id, unifi_id),
            ).fetchone()
            return int(row["id"])

        if connection is not None:
            return write(connection)
        with self.transaction() as db:
            return write(db)

    def print_summary(self, voucher_id: int) -> PrintAuditSummary:
        """Return immutable print totals used before allowing a duplicate."""

        row = self.connection.execute(
            """SELECT COUNT(*) AS jobs,
                      COALESCE(SUM(physical_copies), 0) AS copies,
                      COALESCE(MIN(printed_at), '') AS first_at,
                      COALESCE(MAX(printed_at), '') AS last_at
               FROM voucher_prints WHERE voucher_id=?""",
            (voucher_id,),
        ).fetchone()
        return PrintAuditSummary(
            print_jobs=int(row["jobs"]),
            physical_copies=int(row["copies"]),
            first_printed_at=str(row["first_at"]),
            last_printed_at=str(row["last_at"]),
        )

    def print_summaries_for_codes(
        self,
        *,
        controller_id: int,
        codes: list[str],
    ) -> dict[str, PrintAuditSummary]:
        """Return print facts keyed by canonical voucher code.

        Code lookup is fail-closed: a missing or duplicated code cannot be used
        to decide whether a physical reprint warning is required.
        """

        canonical_codes = {
            str(code).strip().replace("-", "")
            for code in codes
            if str(code).strip()
        }
        result: dict[str, PrintAuditSummary] = {}
        for code in canonical_codes:
            rows = self.connection.execute(
                """SELECT id FROM vouchers
                   WHERE controller_id=? AND REPLACE(code, '-', '')=?
                   ORDER BY id""",
                (controller_id, code),
            ).fetchall()
            if len(rows) != 1:
                raise RuntimeError(
                    "Voucher code is missing or ambiguous in the local database"
                )
            result[code] = self.print_summary(int(rows[0]["id"]))
        return result

    def record_print_audit(
        self,
        *,
        controller_id: int,
        audit_id: str,
        codes: list[str],
        output_file: str,
        document_copies: int,
        printed_at: str,
        windows_user: str,
    ) -> None:
        """Persist one physical-print job exactly once.

        The legacy HMAC history remains the crash boundary during Milestone A.
        SQLite receives the same stable audit_id so an audit-only retry is
        idempotent and can never create an extra reprint sequence.
        """

        normalized_audit_id = str(audit_id).strip()
        normalized_user = str(windows_user).strip()
        normalized_time = str(printed_at).strip()
        if not normalized_audit_id:
            raise ValueError("print audit id is required")
        if not normalized_time:
            raise ValueError("printed_at is required")
        if not normalized_user:
            raise ValueError("windows_user is required")
        if document_copies < 1:
            raise ValueError("document_copies must be positive")

        # PDF/history use the human-readable 12345-67890 form while UniFi
        # may persist the same code without the separator. Resolve by canonical
        # digits so print audit does not depend on presentation formatting.
        counts = Counter(
            str(code).strip().replace("-", "")
            for code in codes
            if str(code).strip()
        )
        if not counts:
            raise ValueError("at least one voucher code is required")

        with self.transaction() as db:
            resolved: dict[str, int] = {}
            for code in counts:
                rows = db.execute(
                    """SELECT id FROM vouchers
                       WHERE controller_id=? AND REPLACE(code, '-', '')=?
                       ORDER BY id""",
                    (controller_id, code),
                ).fetchall()
                if len(rows) != 1:
                    raise RuntimeError(
                        "Voucher code is missing or ambiguous in the local database"
                    )
                resolved[code] = int(rows[0]["id"])

            expected_prints = {
                voucher_id: labels * document_copies
                for code, labels in counts.items()
                for voucher_id in (resolved[code],)
            }
            existing = db.execute(
                "SELECT * FROM print_jobs WHERE print_job_uuid=?",
                (normalized_audit_id,),
            ).fetchone()
            if existing is not None:
                comparable = {
                    "submitted_at": normalized_time,
                    "windows_user": normalized_user,
                    "output_file": str(output_file),
                    "document_copies": document_copies,
                    "status": "AUDITED",
                }
                if any(existing[key] != value for key, value in comparable.items()):
                    raise RuntimeError(
                        "Print audit id already exists with different job data"
                    )
                actual = {
                    int(row["voucher_id"]): int(row["physical_copies"])
                    for row in db.execute(
                        """SELECT voucher_id, physical_copies
                           FROM voucher_prints WHERE print_job_id=?""",
                        (existing["id"],),
                    )
                }
                if actual != expected_prints:
                    raise RuntimeError(
                        "Print audit id already exists with different voucher data"
                    )
                return

            cursor = db.execute(
                """INSERT INTO print_jobs
                   (print_job_uuid, created_at, submitted_at, windows_user,
                    output_file, document_copies, status)
                   VALUES (?, ?, ?, ?, ?, ?, 'AUDITED')""",
                (
                    normalized_audit_id,
                    normalized_time,
                    normalized_time,
                    normalized_user,
                    str(output_file),
                    document_copies,
                ),
            )
            print_job_id = int(cursor.lastrowid)

            for voucher_id, physical_copies in expected_prints.items():
                row = db.execute(
                    """SELECT COALESCE(MAX(print_sequence), 0) AS sequence
                       FROM voucher_prints WHERE voucher_id=?""",
                    (voucher_id,),
                ).fetchone()
                sequence = int(row["sequence"]) + 1
                db.execute(
                    """INSERT INTO voucher_prints
                       (print_job_id, voucher_id, printed_at, windows_user,
                        physical_copies, print_sequence, is_reprint)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        print_job_id,
                        voucher_id,
                        normalized_time,
                        normalized_user,
                        physical_copies,
                        sequence,
                        int(sequence > 1),
                    ),
                )

    @staticmethod
    def encode_event_details(details: dict | None) -> str | None:
        """Serialize optional structured audit details deterministically."""

        if details is None:
            return None
        return json.dumps(details, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
