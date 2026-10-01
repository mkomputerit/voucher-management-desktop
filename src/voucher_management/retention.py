"""Conservative, review-driven retention for Voucher Management 5.0.

Retention and security revocation never run silently. Local minimization acts
only on controller-absent vouchers after an explicit operator threshold.
Security revocation separately identifies printed credentials that remain live
on UniFi with observed zero use beyond a second explicit threshold.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .database import Database


MAX_POLICY_DAYS = 3650
RETENTION_INTRO_KEY = "retention_intro_seen"


@dataclass(frozen=True)
class RetentionPolicy:
    """Two explicit operator-selected lifecycle boundaries."""

    unused_unprinted_days: int
    printed_unused_revoke_days: int
    configured: bool
    protect_used: bool
    protect_printed: bool
    updated_at: str


@dataclass(frozen=True)
class RetentionCandidate:
    """One voucher eligible for explicit operator-reviewed minimization."""

    voucher_id: int
    controller_name: str
    controller_description: str
    assigned_to: str
    created_at: str
    imported_at: str
    expires_at: str
    age_basis: str
    last_synced_at: str
    last_seen_at: str


@dataclass(frozen=True)
class SecurityRevocationCandidate:
    """One printed, unused live voucher eligible for operator-reviewed revocation."""

    voucher_id: int
    controller_id: int
    unifi_id: str
    controller_name: str
    controller_description: str
    assigned_to: str
    first_printed_at: str
    last_printed_at: str
    last_seen_at: str
    print_jobs: int
    physical_copies: int


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


def retention_policy_configured(database: Database) -> bool:
    """Return whether both lifecycle thresholds were explicitly confirmed."""

    row = database.retention_policy()
    if row is None:
        return False
    keys = set(row.keys())
    return (
        "configured" in keys
        and bool(row["configured"])
        and row["unused_unprinted_days"] is not None
        and row["printed_unused_revoke_days"] is not None
    )


def load_retention_policy(database: Database) -> RetentionPolicy:
    """Return the explicit policy or fail closed when configuration is missing."""

    row = database.retention_policy()
    if row is None or not retention_policy_configured(database):
        raise RuntimeError(
            "La policy di conservazione e revoca non è ancora configurata."
        )
    return RetentionPolicy(
        unused_unprinted_days=int(row["unused_unprinted_days"]),
        printed_unused_revoke_days=int(row["printed_unused_revoke_days"]),
        configured=True,
        protect_used=bool(row["protect_used"]),
        protect_printed=bool(row["protect_printed"]),
        updated_at=str(row["updated_at"]),
    )


def configure_retention_policy(
    database: Database,
    *,
    unused_unprinted_days: int,
    printed_unused_revoke_days: int,
    now: str,
) -> RetentionPolicy:
    """Persist both operator-selected thresholds; neither has a software default."""

    local_days = int(unused_unprinted_days)
    revoke_days = int(printed_unused_revoke_days)
    if not 1 <= local_days <= MAX_POLICY_DAYS:
        raise ValueError("local retention days must be between 1 and 3650")
    if not 1 <= revoke_days <= MAX_POLICY_DAYS:
        raise ValueError("security revocation days must be between 1 and 3650")
    stamp = _normalize_now(now).isoformat()
    database.upsert_retention_policy(
        unused_unprinted_days=local_days,
        printed_unused_revoke_days=revoke_days,
        observed_at=stamp,
    )
    return load_retention_policy(database)


def ensure_retention_policy(
    database: Database,
    *,
    now: str,
) -> RetentionPolicy:
    """Compatibility boundary: never invent a threshold on the operator's behalf."""

    _normalize_now(now)
    return load_retention_policy(database)


def update_retention_days(
    database: Database,
    *,
    days: int,
    now: str,
    printed_unused_revoke_days: int | None = None,
) -> RetentionPolicy:
    """Update policy without silently changing the second threshold."""

    current = load_retention_policy(database)
    return configure_retention_policy(
        database,
        unused_unprinted_days=int(days),
        printed_unused_revoke_days=(
            current.printed_unused_revoke_days
            if printed_unused_revoke_days is None
            else int(printed_unused_revoke_days)
        ),
        now=now,
    )


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
                v.assigned_to,
                v.created_at,
                v.imported_at,
                v.expires_at,
                COALESCE(
                    v.revoked_for_security_at,
                    v.last_synced_at,
                    v.last_seen_at,
                    v.created_at,
                    v.imported_at
                ) AS age_basis,
                v.last_synced_at,
                v.last_seen_at,
                v.revoked_for_security_at
           FROM vouchers AS v
           JOIN controllers AS c ON c.id=v.controller_id
           WHERE v.archived_at IS NULL
             AND v.present_on_controller=0
             AND v.usage_observed=1
             AND v.ever_used=0
             AND v.authorized_guest_count=0
             AND (
                 v.revoked_for_security_at IS NOT NULL
                 OR NOT EXISTS (
                     SELECT 1 FROM voucher_prints AS vp
                     WHERE vp.voucher_id=v.id
                 )
             )
             AND COALESCE(
                    v.revoked_for_security_at,
                    v.last_synced_at,
                    v.last_seen_at,
                    v.created_at,
                    v.imported_at
                 ) <= ?
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

    load_retention_policy(database)
    return tuple(
        RetentionCandidate(
            voucher_id=int(row["voucher_id"]),
            controller_name=str(row["controller_name"] or "Controller"),
            controller_description=str(row["name"] or ""),
            assigned_to=str(row["assigned_to"] or ""),
            created_at=str(row["created_at"] or ""),
            imported_at=str(row["imported_at"] or ""),
            expires_at=str(row["expires_at"] or ""),
            age_basis=str(row["age_basis"] or ""),
            last_synced_at=str(row["last_synced_at"] or ""),
            last_seen_at=str(row["last_seen_at"] or ""),
        )
        for row in _candidate_rows(
            database,
            now=now,
            controller_id=controller_id,
        )
    )


def security_revocation_candidates(
    database: Database,
    *,
    now: str,
    controller_id: int | None = None,
) -> tuple[SecurityRevocationCandidate, ...]:
    """Return printed unused live vouchers old enough for explicit revocation."""

    policy = load_retention_policy(database)
    moment = _normalize_now(now)
    cutoff = (
        moment - timedelta(days=policy.printed_unused_revoke_days)
    ).isoformat()
    params: list[object] = [moment.isoformat()]
    controller_clause = ""
    if controller_id is not None:
        controller_clause = "AND v.controller_id=?"
        params.append(int(controller_id))
    params.append(cutoff)

    rows = database.connection.execute(
        f"""SELECT
                v.id AS voucher_id,
                v.controller_id,
                v.unifi_id,
                c.name AS controller_name,
                v.name,
                v.assigned_to,
                v.last_seen_at,
                COUNT(vp.id) AS print_jobs,
                COALESCE(SUM(vp.physical_copies), 0) AS physical_copies,
                (
                    SELECT first_vp.printed_at
                    FROM voucher_prints AS first_vp
                    WHERE first_vp.voucher_id=v.id
                    ORDER BY
                        (julianday(first_vp.printed_at) IS NULL),
                        julianday(first_vp.printed_at) ASC,
                        first_vp.id ASC
                    LIMIT 1
                ) AS first_printed_at,
                (
                    SELECT last_vp.printed_at
                    FROM voucher_prints AS last_vp
                    WHERE last_vp.voucher_id=v.id
                    ORDER BY
                        (julianday(last_vp.printed_at) IS NULL),
                        julianday(last_vp.printed_at) DESC,
                        last_vp.id DESC
                    LIMIT 1
                ) AS last_printed_at
           FROM vouchers AS v
           JOIN controllers AS c ON c.id=v.controller_id
           JOIN voucher_prints AS vp ON vp.voucher_id=v.id
           WHERE v.archived_at IS NULL
             AND v.revoked_for_security_at IS NULL
             AND v.present_on_controller=1
             AND v.usage_observed=1
             AND v.ever_used=0
             AND v.authorized_guest_count=0
             AND v.expired=0
             AND (
                 v.expires_at IS NULL
                 OR julianday(v.expires_at) > julianday(?)
             )
             AND NOT EXISTS (
                 SELECT 1 FROM voucher_events AS review
                 WHERE review.voucher_id=v.id
                   AND review.event_type='LEGACY_IDENTITY_REVIEW_REQUIRED'
             )
             AND NOT EXISTS (
                 SELECT 1 FROM security_revocations AS pending
                 WHERE pending.voucher_id=v.id
                   AND pending.status='PREPARED'
             )
             {controller_clause}
           GROUP BY v.id
           HAVING julianday(v.last_seen_at) >= julianday(last_printed_at)
              AND julianday(last_printed_at) <= julianday(?)
           ORDER BY julianday(last_printed_at) ASC, v.id ASC""",
        tuple(params),
    ).fetchall()

    return tuple(
        SecurityRevocationCandidate(
            voucher_id=int(row["voucher_id"]),
            controller_id=int(row["controller_id"]),
            unifi_id=str(row["unifi_id"]),
            controller_name=str(row["controller_name"] or "Controller"),
            controller_description=str(row["name"] or ""),
            assigned_to=str(row["assigned_to"] or ""),
            first_printed_at=str(row["first_printed_at"] or ""),
            last_printed_at=str(row["last_printed_at"] or ""),
            last_seen_at=str(row["last_seen_at"] or ""),
            print_jobs=int(row["print_jobs"] or 0),
            physical_copies=int(row["physical_copies"] or 0),
        )
        for row in rows
    )


def prepare_security_revocation_operation(
    database: Database,
    *,
    controller_id: int,
    voucher_ids: list[int] | tuple[int, ...],
    operation_uuid: str,
    requested_at: str,
    windows_user: str,
) -> tuple[str, ...]:
    """Revalidate policy eligibility before persisting revocation intent."""

    requested = tuple(dict.fromkeys(int(value) for value in voucher_ids))
    if not requested:
        return ()

    eligible = {
        candidate.voucher_id: candidate
        for candidate in security_revocation_candidates(
            database,
            now=requested_at,
            controller_id=int(controller_id),
        )
    }
    missing = [voucher_id for voucher_id in requested if voucher_id not in eligible]
    if missing:
        raise RuntimeError(
            "Uno o più voucher non soddisfano più i criteri di revoca. "
            "Aggiornare l'elenco e riprovare."
        )

    remote_ids = tuple(eligible[voucher_id].unifi_id for voucher_id in requested)
    database.prepare_security_revocations(
        controller_id=int(controller_id),
        unifi_ids=list(remote_ids),
        operation_uuid=str(operation_uuid),
        requested_at=requested_at,
        requested_by=windows_user,
    )
    return remote_ids


def durable_legacy_generation_blockers(
    database: Database,
    *,
    voucher_ids: list[int] | tuple[int, ...],
) -> frozenset[int]:
    """Return imported legacy generation/print evidence stored in SQLite."""

    requested = tuple(dict.fromkeys(int(value) for value in voucher_ids))
    if not requested:
        return frozenset()
    placeholders = ",".join("?" for _ in requested)
    rows = database.connection.execute(
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
    return frozenset(int(row["voucher_id"]) for row in rows)


def _history_code(value: object) -> str:
    """Return the exact presentation form used by HMAC print history."""

    text = str(value or "").strip()
    canonical = text.replace("-", "")
    if len(canonical) == 10:
        return f"{canonical[:5]}-{canonical[5:]}"
    return text


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
        int(row["id"]): _history_code(row["code"])
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
    # the current installation's live HMAC history.jsonl.
    blocked.update(
        durable_legacy_generation_blockers(
            database,
            voucher_ids=requested,
        )
    )
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
    revoked_ids = {
        int(row["id"])
        for row in database.connection.execute(
            """SELECT id FROM vouchers
               WHERE revoked_for_security_at IS NOT NULL"""
        ).fetchall()
    }
    blocker_targets = [
        candidate.voucher_id
        for candidate in candidates
        if candidate.voucher_id not in revoked_ids
    ]
    blocked = generated_retention_blockers(
        database,
        history=history,
        settings=settings,
        voucher_ids=blocker_targets,
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
    policy = load_retention_policy(database)
    cutoff = (
        _normalize_now(stamp) - timedelta(days=policy.unused_unprinted_days)
    ).isoformat()
    archived: list[int] = []
    skipped: list[int] = []

    with database.transaction() as db:
        for voucher_id in requested:
            revoked = db.execute(
                """SELECT revoked_for_security_at FROM vouchers WHERE id=?""",
                (voucher_id,),
            ).fetchone()
            if (
                voucher_id in generated_blockers
                and (
                    revoked is None
                    or revoked["revoked_for_security_at"] is None
                )
            ):
                skipped.append(voucher_id)
                continue
            row = db.execute(
                """SELECT v.id
                   FROM vouchers AS v
                   WHERE v.id=?
                     AND v.archived_at IS NULL
                     AND v.present_on_controller=0
                     AND v.usage_observed=1
                     AND v.ever_used=0
                     AND v.authorized_guest_count=0
                     AND (
                         v.revoked_for_security_at IS NOT NULL
                         OR NOT EXISTS (
                             SELECT 1 FROM voucher_prints AS vp
                             WHERE vp.voucher_id=v.id
                         )
                     )
                     AND COALESCE(
                            v.revoked_for_security_at,
                            v.last_synced_at,
                            v.last_seen_at,
                            v.created_at,
                            v.imported_at
                         ) <= ?""",
                (voucher_id, cutoff),
            ).fetchone()
            if row is None:
                skipped.append(voucher_id)
                continue

            db.execute(
                """UPDATE vouchers
                   SET code=?, name='', assigned_to='', notes='',
                       is_nominal=NULL, nominality_redacted=1, archived_at=?
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
