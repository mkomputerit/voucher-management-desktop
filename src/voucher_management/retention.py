"""Conservative, review-driven retention for Voucher Management 5.0.

Retention never runs silently. Candidates must be absent from the controller,
unused, never physically printed, old enough under the persisted policy and not
already archived. Approved cleanup preserves the durable voucher row while
removing reusable voucher credentials and operator-entered personal text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .database import Database


DEFAULT_UNUSED_UNPRINTED_DAYS = 180
RETENTION_INTRO_KEY = "retention_intro_seen"


@dataclass(frozen=True)
class RetentionPolicy:
    """Persisted conservative cleanup boundary."""

    unused_unprinted_days: int
    protect_used: bool
    protect_printed: bool
    updated_at: str


@dataclass(frozen=True)
class RetentionCandidate:
    """One voucher eligible for explicit operator-reviewed minimization."""

    voucher_id: int
    controller_name: str
    recipient: str
    created_at: str
    imported_at: str
    expires_at: str
    age_basis: str
    last_synced_at: str


@dataclass(frozen=True)
class RetentionResult:
    """Outcome of one reviewed retention action."""

    archived_ids: tuple[int, ...]
    skipped_ids: tuple[int, ...]


def _normalize_now(now: str) -> datetime:
    value = str(now or "").strip()
    if not value:
        raise ValueError("retention timestamp is required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def ensure_retention_policy(
    database: Database,
    *,
    now: str,
) -> RetentionPolicy:
    """Create the fixed-protection default policy once and return it."""

    stamp = _normalize_now(now).isoformat()
    with database.transaction() as db:
        db.execute(
            """INSERT OR IGNORE INTO retention_policy
               (id, unused_unprinted_days, protect_used, protect_printed, updated_at)
               VALUES (1, ?, 1, 1, ?)""",
            (DEFAULT_UNUSED_UNPRINTED_DAYS, stamp),
        )
    return load_retention_policy(database)


def load_retention_policy(database: Database) -> RetentionPolicy:
    """Return the persisted policy, failing closed if it was not initialized."""

    row = database.connection.execute(
        "SELECT * FROM retention_policy WHERE id=1"
    ).fetchone()
    if row is None:
        raise RuntimeError("retention policy is not initialized")
    return RetentionPolicy(
        unused_unprinted_days=int(row["unused_unprinted_days"]),
        protect_used=bool(row["protect_used"]),
        protect_printed=bool(row["protect_printed"]),
        updated_at=str(row["updated_at"]),
    )


def update_retention_days(
    database: Database,
    *,
    days: int,
    now: str,
) -> RetentionPolicy:
    """Update only the age threshold; used/printed protections are immutable."""

    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError("retention days must be between 1 and 3650")
    stamp = _normalize_now(now).isoformat()
    ensure_retention_policy(database, now=stamp)
    with database.transaction() as db:
        db.execute(
            """UPDATE retention_policy
               SET unused_unprinted_days=?, protect_used=1, protect_printed=1,
                   updated_at=?
               WHERE id=1""",
            (days, stamp),
        )
    return load_retention_policy(database)


def retention_intro_seen(database: Database) -> bool:
    """Return whether the installation-level retention explanation was accepted."""

    row = database.connection.execute(
        "SELECT value FROM settings WHERE key=?",
        (RETENTION_INTRO_KEY,),
    ).fetchone()
    return row is not None and str(row["value"]) == "1"


def mark_retention_intro_seen(database: Database, *, now: str) -> None:
    """Persist completion of the one-time retention explanation."""

    stamp = _normalize_now(now).isoformat()
    with database.transaction() as db:
        db.execute(
            """INSERT INTO settings(key, value, updated_at)
               VALUES (?, '1', ?)
               ON CONFLICT(key) DO UPDATE SET value='1', updated_at=excluded.updated_at""",
            (RETENTION_INTRO_KEY, stamp),
        )


def _candidate_rows(
    database: Database,
    *,
    now: str,
    controller_id: int | None = None,
):
    policy = load_retention_policy(database)
    cutoff = (
        _normalize_now(now) - timedelta(days=policy.unused_unprinted_days)
    ).isoformat()
    params: list[object] = [cutoff]
    controller_clause = ""
    if controller_id is not None:
        controller_clause = "AND v.controller_id=?"
        params.append(int(controller_id))

    return database.connection.execute(
        f"""SELECT
                v.id AS voucher_id,
                c.name AS controller_name,
                v.name,
                v.created_at,
                v.imported_at,
                v.expires_at,
                COALESCE(v.expires_at, v.created_at, v.imported_at) AS age_basis,
                v.last_synced_at
           FROM vouchers AS v
           JOIN controllers AS c ON c.id=v.controller_id
           WHERE v.archived_at IS NULL
             AND v.present_on_controller=0
             AND v.ever_used=0
             AND v.authorized_guest_count=0
             AND NOT EXISTS (
                 SELECT 1 FROM voucher_prints AS vp WHERE vp.voucher_id=v.id
             )
             AND COALESCE(v.expires_at, v.created_at, v.imported_at) <= ?
             {controller_clause}
           ORDER BY age_basis ASC, v.id ASC""",
        tuple(params),
    ).fetchall()


def retention_candidates(
    database: Database,
    *,
    now: str,
    controller_id: int | None = None,
) -> tuple[RetentionCandidate, ...]:
    """Return candidates without changing any voucher or audit record."""

    ensure_retention_policy(database, now=now)
    return tuple(
        RetentionCandidate(
            voucher_id=int(row["voucher_id"]),
            controller_name=str(row["controller_name"] or "Controller"),
            recipient=str(row["name"] or ""),
            created_at=str(row["created_at"] or ""),
            imported_at=str(row["imported_at"] or ""),
            expires_at=str(row["expires_at"] or ""),
            age_basis=str(row["age_basis"] or ""),
            last_synced_at=str(row["last_synced_at"] or ""),
        )
        for row in _candidate_rows(
            database,
            now=now,
            controller_id=controller_id,
        )
    )


def generated_retention_blockers(
    database: Database,
    *,
    history,
    settings: dict,
    voucher_ids: list[int] | tuple[int, ...],
) -> frozenset[int]:
    """Return candidates that still have generated/printed HMAC audit evidence.

    A generated PDF contains the clear voucher credential even before a
    physical print. Retention therefore fails closed when the HMAC history says
    that a candidate has generated-document or print evidence.
    """

    requested = tuple(dict.fromkeys(int(value) for value in voucher_ids))
    if not requested:
        return frozenset()

    placeholders = ",".join("?" for _ in requested)
    rows = database.connection.execute(
        f"""SELECT id, code FROM vouchers
            WHERE id IN ({placeholders})""",
        requested,
    ).fetchall()
    code_by_id = {
        int(row["id"]): str(row["code"])
        for row in rows
        if str(row["code"] or "").strip()
    }
    if not code_by_id:
        return frozenset()

    stats = history.stats_for_codes(
        list(code_by_id.values()),
        settings,
    )
    blocked = {
        voucher_id
        for voucher_id, code in code_by_id.items()
        if (
            stats[code].generated_documents > 0
            or stats[code].generated_copies > 0
            or stats[code].print_jobs > 0
            or stats[code].printed_copies > 0
        )
    }

    # Imported legacy evidence is durable SQLite state and does not appear in
    # the current installation's live HMAC history.jsonl.  It must therefore
    # participate independently in retention protection.  The operational
    # voucher_event covers completed materialization; legacy_audit_events also
    # protects an EVIDENCE_READY run whose materialization has not yet finished.
    sqlite_rows = database.connection.execute(
        f"""SELECT DISTINCT voucher_id
            FROM voucher_events
            WHERE voucher_id IN ({placeholders})
              AND event_type='LEGACY_PDF_GENERATED'
            UNION
            SELECT DISTINCT voucher_id
            FROM legacy_audit_events
            WHERE voucher_id IN ({placeholders})
              AND resolution_status='RESOLVED'
              AND event_type IN ('generate', 'print')""",
        (*requested, *requested),
    ).fetchall()
    blocked.update(int(row["voucher_id"]) for row in sqlite_rows)
    return frozenset(blocked)


def reviewable_retention_candidates(
    database: Database,
    *,
    history,
    settings: dict,
    now: str,
    controller_id: int | None = None,
) -> tuple[RetentionCandidate, ...]:
    """Return only candidates whose credential is absent from PDF audit history."""

    candidates = retention_candidates(
        database,
        now=now,
        controller_id=controller_id,
    )
    blocked = generated_retention_blockers(
        database,
        history=history,
        settings=settings,
        voucher_ids=[candidate.voucher_id for candidate in candidates],
    )
    return tuple(
        candidate
        for candidate in candidates
        if candidate.voucher_id not in blocked
    )


def archive_retention_candidates(
    database: Database,
    *,
    voucher_ids: list[int] | tuple[int, ...],
    archived_at: str,
    windows_user: str,
    history,
    settings: dict,
) -> RetentionResult:
    """Minimize only candidates that still satisfy policy inside the write txn."""

    stamp = _normalize_now(archived_at).isoformat()
    operator = str(windows_user or "").strip()
    if not operator:
        raise ValueError("windows user is required")

    requested = tuple(dict.fromkeys(int(value) for value in voucher_ids))
    if not requested:
        return RetentionResult(archived_ids=(), skipped_ids=())

    generated_blockers = generated_retention_blockers(
        database,
        history=history,
        settings=settings,
        voucher_ids=requested,
    )
    policy = ensure_retention_policy(database, now=stamp)
    cutoff = (
        _normalize_now(stamp) - timedelta(days=policy.unused_unprinted_days)
    ).isoformat()
    archived: list[int] = []
    skipped: list[int] = []

    with database.transaction() as db:
        for voucher_id in requested:
            if voucher_id in generated_blockers:
                skipped.append(voucher_id)
                continue
            row = db.execute(
                """SELECT v.id
                   FROM vouchers AS v
                   WHERE v.id=?
                     AND v.archived_at IS NULL
                     AND v.present_on_controller=0
                     AND v.ever_used=0
                     AND v.authorized_guest_count=0
                     AND NOT EXISTS (
                         SELECT 1 FROM voucher_prints AS vp
                         WHERE vp.voucher_id=v.id
                     )
                     AND COALESCE(v.expires_at, v.created_at, v.imported_at) <= ?""",
                (voucher_id, cutoff),
            ).fetchone()
            if row is None:
                skipped.append(voucher_id)
                continue

            db.execute(
                """UPDATE vouchers
                   SET code=?, name='', assigned_to='', notes='', archived_at=?
                   WHERE id=?""",
                (f"ARCHIVED-{voucher_id}", stamp, voucher_id),
            )
            db.execute(
                """INSERT INTO voucher_events(
                       event_uuid, voucher_id, event_type, occurred_at,
                       source, windows_user, details_json
                   ) VALUES (?, ?, 'RETENTION_ARCHIVED', ?, 'OPERATOR', ?, ?)""",
                (
                    str(uuid4()),
                    voucher_id,
                    stamp,
                    operator,
                    Database.encode_event_details(
                        {
                            "unused_unprinted_days": policy.unused_unprinted_days,
                            "credential_removed": True,
                            "personal_text_removed": True,
                        }
                    ),
                ),
            )
            archived.append(voucher_id)

    return RetentionResult(
        archived_ids=tuple(archived),
        skipped_ids=tuple(skipped),
    )
