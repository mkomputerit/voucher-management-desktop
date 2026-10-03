"""Milestone B tests for non-destructive 4.x audit migration planning."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from voucher_management.legacy_migration import (
    LegacyMigrationError,
    LegacyVoucherCandidate,
    apply_legacy_migration_plan,
    build_legacy_migration_plan,
    execute_legacy_migration,
    legacy_candidates_from_database,
    legacy_migration_plan_needs_reconciliation,
    materialize_resolved_legacy_events,
)
from voucher_management.database import Database


FIXTURE_KEY = "legacy-migration-secret-value"
FINGERPRINT = hashlib.sha256(FIXTURE_KEY.encode("utf-8")).hexdigest()[:16]


def _digest(code: str) -> str:
    return hmac.new(
        FIXTURE_KEY.encode("utf-8"),
        code.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _write_history(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def test_plan_resolves_generate_and_print_rows_from_known_code(tmp_path):
    history = tmp_path / "history.jsonl"
    code = "12345-67890"
    _write_history(
        history,
        [
            {
                "event": "generate",
                "event_id": "event-1",
                "voucher_id": _digest(code),
                "recipient": "Guest",
                "duration_minutes": 60,
                "timestamp": "2026-09-20T10:00:00+00:00",
                "output_file": "Voucher_Test.pdf",
            },
            {
                "event": "print",
                "voucher_id": _digest(code),
                "timestamp": "2026-09-20T10:05:00+00:00",
                "output_file": "Voucher_Test.pdf",
                "document_copies": 2,
                "physical_copies": 2,
                "print_job_id": "abc123",
            },
        ],
    )

    plan = build_legacy_migration_plan(
        history_path=history,
        expected_fingerprint=FINGERPRINT,
        secret=FIXTURE_KEY,
        candidates=[
            LegacyVoucherCandidate(
                controller_id=7,
                unifi_id="voucher-1",
                code="1234567890",
            )
        ],
    )

    assert plan.total_rows == 2
    assert plan.fully_resolved is True
    assert len(plan.resolved) == 2
    assert plan.resolved[0].candidate.unifi_id == "voucher-1"
    assert plan.resolved[1].row.event == "print"
    assert plan.ambiguous == ()
    assert plan.unresolved == ()


def test_preferred_live_controller_shadows_matching_legacy_placeholder(tmp_path):
    db = Database(tmp_path / "candidate-shadow.sqlite")
    db.initialize()
    legacy_controller = db.create_controller(
        name="Archivio precedente",
        api_root="legacy-backup://fixture",
        created_at="2026-09-01T08:00:00+00:00",
    )
    live_controller = db.create_controller(
        name="Reception",
        api_root="https://controller.example",
        created_at="2026-09-02T08:00:00+00:00",
    )
    try:
        db.upsert_voucher(
            controller_id=legacy_controller,
            unifi_id="legacy-backup-fixture-a",
            code="12345-67890",
            imported_at="2026-09-01T08:00:00+00:00",
            last_synced_at="2026-09-01T08:00:00+00:00",
        )
        db.upsert_voucher(
            controller_id=live_controller,
            unifi_id="live-uuid",
            code="1234567890",
            imported_at="2026-09-02T08:00:00+00:00",
            last_synced_at="2026-09-02T08:00:00+00:00",
        )

        candidates = legacy_candidates_from_database(
            db,
            preferred_controller_id=live_controller,
        )

        matching = [
            item
            for item in candidates
            if item.code.replace("-", "") == "1234567890"
        ]
        assert [(item.controller_id, item.unifi_id) for item in matching] == [
            (live_controller, "live-uuid")
        ]
    finally:
        db.close()


def test_materialized_placeholder_converges_to_live_unifi_identity(tmp_path):
    history = tmp_path / "history.jsonl"
    code = "12345-67890"
    _write_history(
        history,
        [
            {
                "event": "generate",
                "event_id": "placeholder-generate",
                "voucher_id": _digest(code),
                "timestamp": "2026-09-20T10:00:00+00:00",
                "output_file": "Voucher_Legacy.pdf",
            },
            {
                "event": "print",
                "voucher_id": _digest(code),
                "timestamp": "2026-09-20T10:05:00+00:00",
                "output_file": "Voucher_Legacy.pdf",
                "document_copies": 1,
                "physical_copies": 1,
                "print_job_id": "placeholder-print",
            },
        ],
    )

    db = Database(tmp_path / "placeholder-merge.sqlite")
    db.initialize()
    legacy_controller = db.create_controller(
        name="Archivio precedente",
        api_root="legacy-backup://fixture",
        created_at="2026-09-20T09:00:00+00:00",
    )
    placeholder_id = db.upsert_voucher(
        controller_id=legacy_controller,
        unifi_id="legacy-backup-fixture-a",
        code="1234567890",
        imported_at="2026-09-20T09:00:00+00:00",
        last_synced_at="2026-09-20T09:00:00+00:00",
    )
    with db.transaction() as tx:
        tx.execute(
            """UPDATE vouchers
               SET present_on_controller=0, usage_observed=0,
                   expiry_observed=0, origin='UNKNOWN'
               WHERE id=?""",
            (placeholder_id,),
        )

    try:
        initial_plan = build_legacy_migration_plan(
            history_path=history,
            expected_fingerprint=FINGERPRINT,
            secret=FIXTURE_KEY,
            candidates=[
                LegacyVoucherCandidate(
                    legacy_controller,
                    "legacy-backup-fixture-a",
                    "1234567890",
                )
            ],
        )
        apply_legacy_migration_plan(
            database=db,
            plan=initial_plan,
            migration_uuid="placeholder-run",
            applied_at="2026-09-20T10:10:00+00:00",
        )
        materialize_resolved_legacy_events(
            database=db,
            materialized_at="2026-09-20T10:11:00+00:00",
            migration_uuid="placeholder-run",
        )
        assert db.connection.execute(
            "SELECT print_state FROM vouchers WHERE id=?",
            (placeholder_id,),
        ).fetchone()["print_state"] == "PRINTED"

        live_controller = db.create_controller(
            name="Reception",
            api_root="https://controller.example",
            created_at="2026-10-01T08:00:00+00:00",
        )
        live_id = db.upsert_voucher(
            controller_id=live_controller,
            unifi_id="live-uuid",
            code="12345-67890",
            name="Guest",
            imported_at="2026-10-01T08:00:00+00:00",
            last_synced_at="2026-10-01T08:00:00+00:00",
        )
        db.record_print_audit(
            controller_id=live_controller,
            audit_id="current-live-print",
            codes=["12345-67890"],
            output_file="current.pdf",
            document_copies=1,
            printed_at="2026-10-01T08:02:00+00:00",
            windows_user=r"PC\operator",
        )

        current_plan = build_legacy_migration_plan(
            history_path=history,
            expected_fingerprint=FINGERPRINT,
            secret=FIXTURE_KEY,
            candidates=legacy_candidates_from_database(
                db,
                preferred_controller_id=live_controller,
            ),
        )
        assert current_plan.fully_resolved is True
        assert {
            item.candidate.unifi_id for item in current_plan.resolved
        } == {"live-uuid"}
        assert (
            legacy_migration_plan_needs_reconciliation(db, current_plan)
            is True
        )

        apply_legacy_migration_plan(
            database=db,
            plan=current_plan,
            migration_uuid="live-reconciliation",
            applied_at="2026-10-01T08:05:00+00:00",
        )
        materialize_resolved_legacy_events(
            database=db,
            materialized_at="2026-10-01T08:06:00+00:00",
            migration_uuid="live-reconciliation",
        )

        assert db.connection.execute(
            "SELECT COUNT(*) FROM vouchers WHERE id=?",
            (placeholder_id,),
        ).fetchone()[0] == 0
        assert {
            int(row["voucher_id"])
            for row in db.connection.execute(
                "SELECT voucher_id FROM legacy_audit_events"
            ).fetchall()
        } == {live_id}
        prints = db.connection.execute(
            """SELECT print_sequence, printed_at, is_reprint
               FROM voucher_prints
               WHERE voucher_id=?
               ORDER BY print_sequence""",
            (live_id,),
        ).fetchall()
        assert [tuple(row) for row in prints] == [
            (1, "2026-09-20T10:05:00+00:00", 0),
            (2, "2026-10-01T08:02:00+00:00", 1),
        ]
        live = db.connection.execute(
            "SELECT print_state FROM vouchers WHERE id=?",
            (live_id,),
        ).fetchone()
        assert live["print_state"] == "PRINTED"
        assert db.connection.execute(
            """SELECT COUNT(*) FROM voucher_events
               WHERE voucher_id=?
                 AND event_type='LEGACY_PLACEHOLDER_MERGED'""",
            (live_id,),
        ).fetchone()[0] == 1
        assert db.connection.execute(
            """SELECT COUNT(*) FROM vouchers
               WHERE REPLACE(code, '-', '')='1234567890'"""
        ).fetchone()[0] == 1
    finally:
        db.close()


def test_placeholder_with_operator_activity_is_not_auto_merged(tmp_path):
    history = tmp_path / "history.jsonl"
    code = "12345-67890"
    _write_history(
        history,
        [
            {
                "event": "generate",
                "event_id": "protected-placeholder",
                "voucher_id": _digest(code),
                "timestamp": "2026-09-20T10:00:00+00:00",
            }
        ],
    )
    db = Database(tmp_path / "protected-placeholder.sqlite")
    db.initialize()
    legacy_controller = db.create_controller(
        name="Archivio precedente",
        api_root="legacy-backup://fixture",
        created_at="2026-09-20T09:00:00+00:00",
    )
    placeholder_id = db.upsert_voucher(
        controller_id=legacy_controller,
        unifi_id="legacy-backup-fixture-a",
        code="1234567890",
        imported_at="2026-09-20T09:00:00+00:00",
        last_synced_at="2026-09-20T09:00:00+00:00",
    )
    initial = build_legacy_migration_plan(
        history_path=history,
        expected_fingerprint=FINGERPRINT,
        secret=FIXTURE_KEY,
        candidates=[
            LegacyVoucherCandidate(
                legacy_controller,
                "legacy-backup-fixture-a",
                "1234567890",
            )
        ],
    )
    apply_legacy_migration_plan(
        database=db,
        plan=initial,
        migration_uuid="protected-old",
        applied_at="2026-09-20T10:01:00+00:00",
    )
    materialize_resolved_legacy_events(
        database=db,
        materialized_at="2026-09-20T10:02:00+00:00",
        migration_uuid="protected-old",
    )
    with db.transaction() as tx:
        tx.execute(
            """INSERT INTO voucher_events(
                   event_uuid, voucher_id, event_type, occurred_at,
                   source, windows_user, details_json
               ) VALUES ('operator-event', ?, 'LOCAL_NOTE_UPDATED',
                         '2026-09-21T10:00:00+00:00',
                         'OPERATOR', 'PC\\operator', NULL)""",
            (placeholder_id,),
        )

    try:
        live_controller = db.create_controller(
            name="Reception",
            api_root="https://controller.example",
            created_at="2026-10-01T08:00:00+00:00",
        )
        db.upsert_voucher(
            controller_id=live_controller,
            unifi_id="live-uuid",
            code="1234567890",
            imported_at="2026-10-01T08:00:00+00:00",
            last_synced_at="2026-10-01T08:00:00+00:00",
        )
        current = build_legacy_migration_plan(
            history_path=history,
            expected_fingerprint=FINGERPRINT,
            secret=FIXTURE_KEY,
            candidates=legacy_candidates_from_database(
                db,
                preferred_controller_id=live_controller,
            ),
        )

        with pytest.raises(
            LegacyMigrationError,
            match="confligge",
        ):
            legacy_migration_plan_needs_reconciliation(db, current)

        assert db.connection.execute(
            "SELECT COUNT(*) FROM vouchers WHERE id=?",
            (placeholder_id,),
        ).fetchone()[0] == 1
    finally:
        db.close()


def test_plan_ignores_modern_rows_with_stable_voucher_identity(tmp_path):
    history = tmp_path / "history.jsonl"
    code = "12345-67890"
    modern_ref = "a" * 64
    _write_history(
        history,
        [
            {
                "event": "generate",
                "event_id": "modern-event",
                "voucher_id": _digest(code),
                "voucher_ref": modern_ref,
                "timestamp": "2026-09-20T09:00:00+00:00",
                "output_file": "Voucher_Modern.pdf",
            },
            {
                "event": "print",
                "voucher_id": _digest(code),
                "timestamp": "2026-09-20T09:05:00+00:00",
                "output_file": "Voucher_Modern.pdf",
                "document_copies": 1,
                "physical_copies": 1,
                "print_job_id": "modern-print",
                "voucher_ref": modern_ref,
            },
            {
                "event": "print",
                "voucher_id": _digest(code),
                "timestamp": "2026-09-19T10:05:00+00:00",
                "output_file": "Voucher_Legacy.pdf",
                "document_copies": 1,
                "physical_copies": 1,
                "print_job_id": "legacy-print",
            },
        ],
    )

    plan = build_legacy_migration_plan(
        history_path=history,
        expected_fingerprint=FINGERPRINT,
        secret=FIXTURE_KEY,
        candidates=[
            LegacyVoucherCandidate(
                controller_id=7,
                unifi_id="voucher-1",
                code=code,
            )
        ],
    )

    assert plan.total_rows == 1
    assert len(plan.resolved) == 1
    assert plan.resolved[0].row.payload["print_job_id"] == "legacy-print"


def test_plan_rejects_malformed_modern_stable_identity(tmp_path):
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "event": "print",
                "voucher_id": _digest("12345-67890"),
                "voucher_ref": "not-a-valid-hmac",
                "timestamp": "2026-09-20T10:05:00+00:00",
                "output_file": "Voucher_Modern.pdf",
                "document_copies": 1,
                "physical_copies": 1,
                "print_job_id": "modern-print",
            }
        ],
    )

    with pytest.raises(
        LegacyMigrationError,
        match="Identità voucher moderna non valida",
    ):
        build_legacy_migration_plan(
            history_path=history,
            expected_fingerprint=FINGERPRINT,
            secret=FIXTURE_KEY,
            candidates=[
                LegacyVoucherCandidate(
                    controller_id=7,
                    unifi_id="voucher-1",
                    code="12345-67890",
                )
            ],
        )


def test_plan_preserves_unknown_hmac_as_unresolved_evidence(tmp_path):
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "event": "generate",
                "voucher_id": _digest("99999-00000"),
                "timestamp": "2026-09-20T10:00:00+00:00",
                "output_file": "Voucher_Old.pdf",
            }
        ],
    )

    plan = build_legacy_migration_plan(
        history_path=history,
        expected_fingerprint=FINGERPRINT,
        secret=FIXTURE_KEY,
        candidates=[
            LegacyVoucherCandidate(
                controller_id=1,
                unifi_id="current",
                code="12345-67890",
            )
        ],
    )

    assert plan.resolved == ()
    assert plan.ambiguous == ()
    assert len(plan.unresolved) == 1
    assert plan.unresolved[0].reason == "no_known_code"
    assert plan.unresolved[0].row.line_number == 1


def test_same_digest_on_two_voucher_identities_is_ambiguous(tmp_path):
    history = tmp_path / "history.jsonl"
    code = "55555-66666"
    _write_history(
        history,
        [
            {
                "event": "generate",
                "voucher_id": _digest(code),
                "timestamp": "2026-09-20T10:00:00+00:00",
                "output_file": "Voucher_Ambiguous.pdf",
            }
        ],
    )

    plan = build_legacy_migration_plan(
        history_path=history,
        expected_fingerprint=FINGERPRINT,
        secret=FIXTURE_KEY,
        candidates=[
            LegacyVoucherCandidate(1, "controller-a-id", code),
            LegacyVoucherCandidate(2, "controller-b-id", code),
        ],
    )

    assert plan.resolved == ()
    assert plan.unresolved == ()
    assert len(plan.ambiguous) == 1
    assert {
        candidate.key
        for candidate in plan.ambiguous[0].candidates
    } == {
        (1, "controller-a-id"),
        (2, "controller-b-id"),
    }



def test_same_unifi_id_on_different_controllers_is_still_ambiguous(tmp_path):
    history = tmp_path / "history.jsonl"
    code = "44444-55555"
    _write_history(
        history,
        [
            {
                "event": "generate",
                "voucher_id": _digest(code),
                "timestamp": "2026-09-20T10:00:00+00:00",
            }
        ],
    )

    plan = build_legacy_migration_plan(
        history_path=history,
        expected_fingerprint=FINGERPRINT,
        secret=FIXTURE_KEY,
        candidates=[
            LegacyVoucherCandidate(1, "same-unifi-id", code),
            LegacyVoucherCandidate(2, "same-unifi-id", code),
        ],
    )

    assert plan.resolved == ()
    assert plan.unresolved == ()
    assert len(plan.ambiguous) == 1
    assert {
        candidate.key
        for candidate in plan.ambiguous[0].candidates
    } == {
        (1, "same-unifi-id"),
        (2, "same-unifi-id"),
    }


def test_duplicate_candidate_spelling_does_not_create_false_ambiguity(tmp_path):
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "event": "generate",
                "voucher_id": _digest("11111-22222"),
                "timestamp": "2026-09-20T10:00:00+00:00",
            }
        ],
    )

    plan = build_legacy_migration_plan(
        history_path=history,
        expected_fingerprint=FINGERPRINT,
        secret=FIXTURE_KEY,
        candidates=[
            LegacyVoucherCandidate(1, "v1", "11111-22222"),
            LegacyVoucherCandidate(1, "v1", "1111122222"),
        ],
    )

    assert len(plan.resolved) == 1
    assert plan.ambiguous == ()


@pytest.mark.parametrize(
    "expected,secret",
    [
        ("deadbeefdeadbeef", FIXTURE_KEY),
        (FINGERPRINT, "different-secret"),
        ("", FIXTURE_KEY),
    ],
)
def test_identity_mismatch_fails_before_history_is_migrated(
    tmp_path,
    expected,
    secret,
):
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "event": "generate",
                "voucher_id": _digest("12345-67890"),
                "timestamp": "2026-09-20T10:00:00+00:00",
            }
        ],
    )

    with pytest.raises(
        LegacyMigrationError,
        match="Identità HMAC",
    ):
        build_legacy_migration_plan(
            history_path=history,
            expected_fingerprint=expected,
            secret=secret,
            candidates=[],
        )


@pytest.mark.parametrize(
    "payload",
    [
        "{broken-json\n",
        json.dumps(
            {
                "event": "unknown",
                "voucher_id": "0" * 64,
                "timestamp": "t",
            }
        )
        + "\n",
        json.dumps(
            {
                "event": "print",
                "voucher_id": "0" * 64,
                "timestamp": "t",
                "physical_copies": 0,
            }
        )
        + "\n",
        json.dumps(
            {
                "event": "generate",
                "voucher_id": "not-a-digest",
                "timestamp": "t",
            }
        )
        + "\n",
    ],
)
def test_corrupt_or_unsupported_history_fails_closed(tmp_path, payload):
    history = tmp_path / "history.jsonl"
    history.write_text(payload, encoding="utf-8")

    with pytest.raises(LegacyMigrationError):
        build_legacy_migration_plan(
            history_path=history,
            expected_fingerprint=FINGERPRINT,
            secret=FIXTURE_KEY,
            candidates=[],
        )


def test_planning_is_read_only_and_idempotent(tmp_path):
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "event": "generate",
                "voucher_id": _digest("12345-67890"),
                "timestamp": "2026-09-20T10:00:00+00:00",
            }
        ],
    )
    before = history.read_bytes()
    args = dict(
        history_path=history,
        expected_fingerprint=FINGERPRINT,
        secret=FIXTURE_KEY,
        candidates=[
            LegacyVoucherCandidate(1, "v1", "1234567890"),
        ],
    )

    first = build_legacy_migration_plan(**args)
    second = build_legacy_migration_plan(**args)

    assert first == second
    assert history.read_bytes() == before


def _database_with_voucher(tmp_path, *, code="1234567890"):
    db = Database(tmp_path / "voucher-management.db")
    db.initialize()
    controller_id = db.create_controller(
        name="Migration fixture",
        api_root="https://controller.example",
        created_at="2026-09-26T10:00:00+00:00",
    )
    voucher_id = db.upsert_voucher(
        controller_id=controller_id,
        unifi_id="legacy-voucher-1",
        code=code,
        imported_at="2026-09-26T10:00:00+00:00",
        last_synced_at="2026-09-26T10:00:00+00:00",
    )
    return db, controller_id, voucher_id


def test_apply_persists_resolved_and_unresolved_evidence_atomically(tmp_path):
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "event": "generate",
                "event_id": "resolved-event",
                "voucher_id": _digest("12345-67890"),
                "timestamp": "2026-09-20T10:00:00+00:00",
            },
            {
                "event": "generate",
                "event_id": "unresolved-event",
                "voucher_id": _digest("99999-00000"),
                "timestamp": "2026-09-20T11:00:00+00:00",
            },
        ],
    )
    db, controller_id, voucher_id = _database_with_voucher(tmp_path)
    try:
        plan = build_legacy_migration_plan(
            history_path=history,
            expected_fingerprint=FINGERPRINT,
            secret=FIXTURE_KEY,
            candidates=[
                LegacyVoucherCandidate(
                    controller_id,
                    "legacy-voucher-1",
                    "1234567890",
                )
            ],
        )

        result = apply_legacy_migration_plan(
            database=db,
            plan=plan,
            migration_uuid="migration-1",
            applied_at="2026-09-26T10:05:00+00:00",
        )

        assert result.total_rows == 2
        assert result.resolved_rows == 1
        assert result.unresolved_rows == 1
        rows = db.connection.execute(
            """SELECT legacy_event_key, resolution_status, voucher_id
               FROM legacy_audit_events ORDER BY source_line"""
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("generate:resolved-event", "RESOLVED", voucher_id),
            ("generate:unresolved-event", "UNRESOLVED", None),
        ]
        run = db.connection.execute(
            "SELECT * FROM migration_runs WHERE migration_uuid='migration-1'"
        ).fetchone()
        assert run["status"] == "EVIDENCE_READY"
        assert run["source_history_sha256"] == plan.source_history_sha256
    finally:
        db.close()


def test_reconciliation_gate_stops_after_resolved_evidence_is_materialized(tmp_path):
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "event": "print",
                "voucher_id": _digest("12345-67890"),
                "timestamp": "2026-09-20T10:05:00+00:00",
                "output_file": "Voucher_Legacy.pdf",
                "document_copies": 1,
                "physical_copies": 1,
                "print_job_id": "legacy-gate-print",
            }
        ],
    )
    db, controller_id, _voucher_id = _database_with_voucher(tmp_path)
    try:
        plan = build_legacy_migration_plan(
            history_path=history,
            expected_fingerprint=FINGERPRINT,
            secret=FIXTURE_KEY,
            candidates=[
                LegacyVoucherCandidate(
                    controller_id,
                    "legacy-voucher-1",
                    "1234567890",
                )
            ],
        )
        assert legacy_migration_plan_needs_reconciliation(db, plan) is True

        apply_legacy_migration_plan(
            database=db,
            plan=plan,
            migration_uuid="gate-run",
            applied_at="2026-09-26T10:05:00+00:00",
        )
        assert legacy_migration_plan_needs_reconciliation(db, plan) is True

        materialize_resolved_legacy_events(
            database=db,
            materialized_at="2026-09-26T10:06:00+00:00",
            migration_uuid="gate-run",
        )
        assert legacy_migration_plan_needs_reconciliation(db, plan) is False
    finally:
        db.close()


def test_reconciliation_gate_reopens_when_unresolved_evidence_becomes_resolvable(tmp_path):
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "event": "generate",
                "event_id": "later-resolvable",
                "voucher_id": _digest("99999-00000"),
                "timestamp": "2026-09-20T10:00:00+00:00",
            }
        ],
    )
    db, _controller_id, _voucher_id = _database_with_voucher(tmp_path)
    try:
        unresolved = build_legacy_migration_plan(
            history_path=history,
            expected_fingerprint=FINGERPRINT,
            secret=FIXTURE_KEY,
            candidates=[],
        )
        apply_legacy_migration_plan(
            database=db,
            plan=unresolved,
            migration_uuid="unresolved-run",
            applied_at="2026-09-26T10:05:00+00:00",
        )
        assert (
            legacy_migration_plan_needs_reconciliation(db, unresolved)
            is False
        )

        controller_id = db.connection.execute(
            "SELECT id FROM controllers LIMIT 1"
        ).fetchone()["id"]
        db.upsert_voucher(
            controller_id=int(controller_id),
            unifi_id="later-voucher",
            code="9999900000",
            imported_at="2026-09-27T08:00:00+00:00",
            last_synced_at="2026-09-27T08:00:00+00:00",
        )
        resolved = build_legacy_migration_plan(
            history_path=history,
            expected_fingerprint=FINGERPRINT,
            secret=FIXTURE_KEY,
            candidates=[
                LegacyVoucherCandidate(
                    int(controller_id),
                    "later-voucher",
                    "9999900000",
                )
            ],
        )
        assert legacy_migration_plan_needs_reconciliation(db, resolved) is True
    finally:
        db.close()


def test_apply_same_migration_uuid_is_idempotent(tmp_path):
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "event": "generate",
                "event_id": "event-1",
                "voucher_id": _digest("12345-67890"),
                "timestamp": "2026-09-20T10:00:00+00:00",
            }
        ],
    )
    db, controller_id, _voucher_id = _database_with_voucher(tmp_path)
    try:
        plan = build_legacy_migration_plan(
            history_path=history,
            expected_fingerprint=FINGERPRINT,
            secret=FIXTURE_KEY,
            candidates=[
                LegacyVoucherCandidate(
                    controller_id,
                    "legacy-voucher-1",
                    "1234567890",
                )
            ],
        )
        first = apply_legacy_migration_plan(
            database=db,
            plan=plan,
            migration_uuid="migration-repeat",
            applied_at="2026-09-26T10:05:00+00:00",
        )
        second = apply_legacy_migration_plan(
            database=db,
            plan=plan,
            migration_uuid="migration-repeat",
            applied_at="2026-09-26T10:06:00+00:00",
        )

        assert first.already_applied is False
        assert second.already_applied is True
        assert (
            db.connection.execute(
                "SELECT COUNT(*) FROM legacy_audit_events"
            ).fetchone()[0]
            == 1
        )
        assert (
            db.connection.execute(
                "SELECT COUNT(*) FROM migration_runs"
            ).fetchone()[0]
            == 1
        )
    finally:
        db.close()


def test_later_plan_keeps_resolution_and_updates_last_migration_uuid(
    tmp_path,
):
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "event": "generate",
                "event_id": "stable-event",
                "voucher_id": _digest("12345-67890"),
                "timestamp": "2026-09-20T10:00:00+00:00",
            }
        ],
    )
    db, controller_id, voucher_id = _database_with_voucher(tmp_path)
    try:
        plan = build_legacy_migration_plan(
            history_path=history,
            expected_fingerprint=FINGERPRINT,
            secret=FIXTURE_KEY,
            candidates=[
                LegacyVoucherCandidate(
                    controller_id,
                    "legacy-voucher-1",
                    "1234567890",
                )
            ],
        )
        apply_legacy_migration_plan(
            database=db,
            plan=plan,
            migration_uuid="migration-stable-1",
            applied_at="2026-09-26T10:05:00+00:00",
        )
        apply_legacy_migration_plan(
            database=db,
            plan=plan,
            migration_uuid="migration-stable-2",
            applied_at="2026-09-26T10:10:00+00:00",
        )

        row = db.connection.execute(
            """SELECT resolution_status, voucher_id, first_migration_uuid,
                      last_migration_uuid
               FROM legacy_audit_events
               WHERE legacy_event_key='generate:stable-event'"""
        ).fetchone()
        assert row["resolution_status"] == "RESOLVED"
        assert row["voucher_id"] == voucher_id
        assert row["first_migration_uuid"] == "migration-stable-1"
        assert row["last_migration_uuid"] == "migration-stable-2"
    finally:
        db.close()


def test_later_plan_can_resolve_previously_unresolved_evidence(tmp_path):
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "event": "generate",
                "event_id": "late-event",
                "voucher_id": _digest("12345-67890"),
                "timestamp": "2026-09-20T10:00:00+00:00",
            }
        ],
    )
    db, controller_id, voucher_id = _database_with_voucher(tmp_path)
    try:
        unresolved_plan = build_legacy_migration_plan(
            history_path=history,
            expected_fingerprint=FINGERPRINT,
            secret=FIXTURE_KEY,
            candidates=[],
        )
        apply_legacy_migration_plan(
            database=db,
            plan=unresolved_plan,
            migration_uuid="migration-unresolved",
            applied_at="2026-09-26T10:05:00+00:00",
        )

        resolved_plan = build_legacy_migration_plan(
            history_path=history,
            expected_fingerprint=FINGERPRINT,
            secret=FIXTURE_KEY,
            candidates=[
                LegacyVoucherCandidate(
                    controller_id,
                    "legacy-voucher-1",
                    "1234567890",
                )
            ],
        )
        apply_legacy_migration_plan(
            database=db,
            plan=resolved_plan,
            migration_uuid="migration-resolved",
            applied_at="2026-09-26T10:10:00+00:00",
        )

        row = db.connection.execute(
            """SELECT resolution_status, voucher_id, first_migration_uuid,
                      last_migration_uuid
               FROM legacy_audit_events
               WHERE legacy_event_key='generate:late-event'"""
        ).fetchone()
        assert row["resolution_status"] == "RESOLVED"
        assert row["voucher_id"] == voucher_id
        assert row["first_migration_uuid"] == "migration-unresolved"
        assert row["last_migration_uuid"] == "migration-resolved"
    finally:
        db.close()


def test_stale_resolved_plan_rolls_back_all_evidence_writes(tmp_path):
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "event": "generate",
                "event_id": "stale-event",
                "voucher_id": _digest("12345-67890"),
                "timestamp": "2026-09-20T10:00:00+00:00",
            }
        ],
    )
    db, controller_id, voucher_id = _database_with_voucher(tmp_path)
    try:
        plan = build_legacy_migration_plan(
            history_path=history,
            expected_fingerprint=FINGERPRINT,
            secret=FIXTURE_KEY,
            candidates=[
                LegacyVoucherCandidate(
                    controller_id,
                    "legacy-voucher-1",
                    "1234567890",
                )
            ],
        )
        db.connection.execute(
            "UPDATE vouchers SET code='0000000000' WHERE id=?",
            (voucher_id,),
        )
        db.connection.commit()

        with pytest.raises(
            LegacyMigrationError,
            match="Codice voucher cambiato",
        ):
            apply_legacy_migration_plan(
                database=db,
                plan=plan,
                migration_uuid="migration-stale",
                applied_at="2026-09-26T10:05:00+00:00",
            )

        assert (
            db.connection.execute(
                "SELECT COUNT(*) FROM migration_runs"
            ).fetchone()[0]
            == 0
        )
        assert (
            db.connection.execute(
                "SELECT COUNT(*) FROM legacy_audit_events"
            ).fetchone()[0]
            == 0
        )
    finally:
        db.close()


def test_duplicate_stable_event_identity_fails_closed(tmp_path):
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "event": "generate",
                "event_id": "duplicate-event",
                "voucher_id": _digest("12345-67890"),
                "timestamp": "2026-09-20T10:00:00+00:00",
            },
            {
                "event": "generate",
                "event_id": "duplicate-event",
                "voucher_id": _digest("99999-00000"),
                "timestamp": "2026-09-20T11:00:00+00:00",
            },
        ],
    )

    with pytest.raises(
        LegacyMigrationError,
        match="Identità evento legacy duplicata",
    ):
        build_legacy_migration_plan(
            history_path=history,
            expected_fingerprint=FINGERPRINT,
            secret=FIXTURE_KEY,
            candidates=[],
        )


def _plan_and_apply(
    *,
    db,
    history,
    controller_id,
    migration_uuid="migration-materialize",
):
    plan = build_legacy_migration_plan(
        history_path=history,
        expected_fingerprint=FINGERPRINT,
        secret=FIXTURE_KEY,
        candidates=[
            LegacyVoucherCandidate(
                controller_id,
                "legacy-voucher-1",
                "1234567890",
            )
        ],
    )
    apply_legacy_migration_plan(
        database=db,
        plan=plan,
        migration_uuid=migration_uuid,
        applied_at="2026-09-26T10:20:00+00:00",
    )
    return plan


def test_materializes_resolved_generate_event_idempotently(tmp_path):
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "event": "generate",
                "event_id": "generation-1",
                "voucher_id": _digest("12345-67890"),
                "recipient": "Reception",
                "duration_minutes": 120,
                "timestamp": "2026-09-20T10:00:00+00:00",
                "output_file": r"C:\\Old\\Voucher_Test.pdf",
                "reprint": False,
            }
        ],
    )
    db, controller_id, voucher_id = _database_with_voucher(tmp_path)
    try:
        _plan_and_apply(
            db=db,
            history=history,
            controller_id=controller_id,
        )

        first = materialize_resolved_legacy_events(
            database=db,
            materialized_at="2026-09-26T10:25:00+00:00",
        )
        second = materialize_resolved_legacy_events(
            database=db,
            materialized_at="2026-09-26T10:30:00+00:00",
        )

        assert first.generated_events == 1
        assert second.generated_events == 1
        assert db.connection.execute(
            "SELECT COUNT(*) FROM voucher_events"
        ).fetchone()[0] == 1
        event = db.connection.execute(
            "SELECT * FROM voucher_events"
        ).fetchone()
        assert event["voucher_id"] == voucher_id
        assert event["event_type"] == "LEGACY_PDF_GENERATED"
        details = json.loads(event["details_json"])
        assert details["output_file"] == "Voucher_Test.pdf"
        evidence = db.connection.execute(
            "SELECT materialized_at FROM legacy_audit_events"
        ).fetchone()
        assert evidence["materialized_at"] == "2026-09-26T10:25:00+00:00"
    finally:
        db.close()


def test_materializes_legacy_print_and_reorders_existing_v5_sequence(tmp_path):
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "event": "print",
                "voucher_id": _digest("12345-67890"),
                "timestamp": "2026-09-20T10:00:00+00:00",
                "output_file": "Voucher_Legacy.pdf",
                "document_copies": 1,
                "physical_copies": 1,
                "print_job_id": "legacy-job-1",
            }
        ],
    )
    db, controller_id, voucher_id = _database_with_voucher(tmp_path)
    try:
        db.record_print_audit(
            controller_id=controller_id,
            audit_id="current-job-1",
            codes=["12345-67890"],
            output_file="Voucher_Current.pdf",
            document_copies=1,
            printed_at="2026-09-25T10:00:00+00:00",
            windows_user="operator",
        )
        _plan_and_apply(
            db=db,
            history=history,
            controller_id=controller_id,
        )

        result = materialize_resolved_legacy_events(
            database=db,
            materialized_at="2026-09-26T10:25:00+00:00",
        )

        assert result.print_rows == 1
        rows = db.connection.execute(
            """SELECT pj.print_job_uuid, vp.print_sequence, vp.is_reprint
               FROM voucher_prints AS vp
               JOIN print_jobs AS pj ON pj.id=vp.print_job_id
               WHERE vp.voucher_id=?
               ORDER BY vp.print_sequence""",
            (voucher_id,),
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("legacy-job-1", 1, 0),
            ("current-job-1", 2, 1),
        ]
    finally:
        db.close()



def test_materializes_print_without_legacy_print_job_id_idempotently(tmp_path):
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "event": "print",
                "voucher_id": _digest("12345-67890"),
                "timestamp": "2026-09-20T09:00:00+00:00",
                "output_file": "Voucher_NoJobId.pdf",
                "document_copies": 2,
                "physical_copies": 2,
            }
        ],
    )
    db, controller_id, voucher_id = _database_with_voucher(tmp_path)
    try:
        _plan_and_apply(
            db=db,
            history=history,
            controller_id=controller_id,
        )

        first = materialize_resolved_legacy_events(
            database=db,
            materialized_at="2026-09-26T10:30:00+00:00",
        )
        rows = db.connection.execute(
            """SELECT pj.print_job_uuid, vp.physical_copies
               FROM voucher_prints AS vp
               JOIN print_jobs AS pj ON pj.id=vp.print_job_id
               WHERE vp.voucher_id=?""",
            (voucher_id,),
        ).fetchall()
        assert first.print_rows == 1
        assert len(rows) == 1
        assert rows[0]["print_job_uuid"].startswith("legacy-")
        assert rows[0]["physical_copies"] == 2
        synthetic_job_id = rows[0]["print_job_uuid"]

        second = materialize_resolved_legacy_events(
            database=db,
            materialized_at="2026-09-26T10:31:00+00:00",
        )
        rows_after = db.connection.execute(
            """SELECT pj.print_job_uuid
               FROM voucher_prints AS vp
               JOIN print_jobs AS pj ON pj.id=vp.print_job_id
               WHERE vp.voucher_id=?""",
            (voucher_id,),
        ).fetchall()
        assert second.print_rows == 1
        assert [row["print_job_uuid"] for row in rows_after] == [
            synthetic_job_id
        ]
    finally:
        db.close()



def test_two_legacy_print_rows_without_job_id_become_distinct_jobs(tmp_path):
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "event": "print",
                "voucher_id": _digest("12345-67890"),
                "timestamp": "2026-09-20T09:00:00+00:00",
                "output_file": "Voucher_Old_A.pdf",
                "document_copies": 1,
                "physical_copies": 1,
            },
            {
                "event": "print",
                "voucher_id": _digest("12345-67890"),
                "timestamp": "2026-09-20T09:05:00+00:00",
                "output_file": "Voucher_Old_B.pdf",
                "document_copies": 1,
                "physical_copies": 1,
            },
        ],
    )
    db, controller_id, voucher_id = _database_with_voucher(tmp_path)
    try:
        _plan_and_apply(
            db=db,
            history=history,
            controller_id=controller_id,
        )

        result = materialize_resolved_legacy_events(
            database=db,
            materialized_at="2026-09-26T10:32:00+00:00",
        )

        rows = db.connection.execute(
            """SELECT pj.print_job_uuid, vp.print_sequence
               FROM voucher_prints AS vp
               JOIN print_jobs AS pj ON pj.id=vp.print_job_id
               WHERE vp.voucher_id=?
               ORDER BY vp.print_sequence""",
            (voucher_id,),
        ).fetchall()

        assert result.print_rows == 2
        assert len(rows) == 2
        assert rows[0]["print_job_uuid"].startswith("legacy-")
        assert rows[1]["print_job_uuid"].startswith("legacy-")
        assert rows[0]["print_job_uuid"] != rows[1]["print_job_uuid"]
        assert [row["print_sequence"] for row in rows] == [1, 2]
    finally:
        db.close()


def test_generate_without_event_id_materializes_end_to_end(tmp_path):
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "event": "generate",
                "voucher_id": _digest("12345-67890"),
                "recipient": "Legacy guest",
                "duration_minutes": 60,
                "timestamp": "2026-09-20T08:30:00+00:00",
                "output_file": "Voucher_PreEventId.pdf",
            }
        ],
    )
    db, controller_id, voucher_id = _database_with_voucher(tmp_path)
    try:
        _plan_and_apply(
            db=db,
            history=history,
            controller_id=controller_id,
        )

        first = materialize_resolved_legacy_events(
            database=db,
            materialized_at="2026-09-26T10:33:00+00:00",
        )
        second = materialize_resolved_legacy_events(
            database=db,
            materialized_at="2026-09-26T10:34:00+00:00",
        )

        rows = db.connection.execute(
            """SELECT event_uuid, event_type, source, details_json
               FROM voucher_events
               WHERE voucher_id=?""",
            (voucher_id,),
        ).fetchall()

        assert first.generated_events == 1
        assert second.generated_events == 1
        assert len(rows) == 1
        assert rows[0]["event_uuid"].startswith("legacy-generate-")
        assert rows[0]["event_type"] == "LEGACY_PDF_GENERATED"
        assert rows[0]["source"] == "MIGRATION"
        details = json.loads(rows[0]["details_json"])
        assert details["output_file"] == "Voucher_PreEventId.pdf"
        assert details["recipient"] == "Legacy guest"
    finally:
        db.close()


def test_materialization_reuses_existing_milestone_a_print_job(tmp_path):
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "event": "print",
                "voucher_id": _digest("12345-67890"),
                "timestamp": "2026-09-25T10:00:00+00:00",
                "output_file": "Voucher_Current.pdf",
                "document_copies": 2,
                "physical_copies": 2,
                "print_job_id": "shared-audit-id",
            }
        ],
    )
    db, controller_id, _voucher_id = _database_with_voucher(tmp_path)
    try:
        db.record_print_audit(
            controller_id=controller_id,
            audit_id="shared-audit-id",
            codes=["12345-67890"],
            output_file="Voucher_Current.pdf",
            document_copies=2,
            printed_at="2026-09-25T10:00:00+00:00",
            windows_user="operator",
        )
        _plan_and_apply(
            db=db,
            history=history,
            controller_id=controller_id,
        )

        materialize_resolved_legacy_events(
            database=db,
            materialized_at="2026-09-26T10:25:00+00:00",
        )

        assert db.connection.execute(
            "SELECT COUNT(*) FROM print_jobs"
        ).fetchone()[0] == 1
        assert db.connection.execute(
            "SELECT COUNT(*) FROM voucher_prints"
        ).fetchone()[0] == 1
    finally:
        db.close()


def test_unresolved_evidence_is_never_materialized(tmp_path):
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "event": "print",
                "voucher_id": _digest("99999-00000"),
                "timestamp": "2026-09-20T10:00:00+00:00",
                "physical_copies": 1,
                "document_copies": 1,
                "print_job_id": "unknown-job",
            }
        ],
    )
    db, _controller_id, _voucher_id = _database_with_voucher(tmp_path)
    try:
        plan = build_legacy_migration_plan(
            history_path=history,
            expected_fingerprint=FINGERPRINT,
            secret=FIXTURE_KEY,
            candidates=[],
        )
        apply_legacy_migration_plan(
            database=db,
            plan=plan,
            migration_uuid="migration-unresolved-materialize",
            applied_at="2026-09-26T10:20:00+00:00",
        )

        result = materialize_resolved_legacy_events(
            database=db,
            materialized_at="2026-09-26T10:25:00+00:00",
        )

        assert result.print_rows == 0
        assert db.connection.execute(
            "SELECT COUNT(*) FROM print_jobs"
        ).fetchone()[0] == 0
        assert db.connection.execute(
            "SELECT materialized_at FROM legacy_audit_events"
        ).fetchone()[0] is None
    finally:
        db.close()


def test_conflicting_existing_print_job_rolls_back_materialization(tmp_path):
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "event": "print",
                "voucher_id": _digest("12345-67890"),
                "timestamp": "2026-09-20T10:00:00+00:00",
                "output_file": "Voucher_Legacy.pdf",
                "document_copies": 1,
                "physical_copies": 1,
                "print_job_id": "conflict-job",
            }
        ],
    )
    db, controller_id, _voucher_id = _database_with_voucher(tmp_path)
    try:
        _plan_and_apply(
            db=db,
            history=history,
            controller_id=controller_id,
        )
        db.connection.execute(
            """INSERT INTO print_jobs
               (print_job_uuid, created_at, submitted_at, windows_user,
                output_file, document_copies, status)
               VALUES ('conflict-job', 't', 'different', 'operator',
                       'Other.pdf', 1, 'AUDITED')"""
        )
        db.connection.commit()

        with pytest.raises(
            LegacyMigrationError,
            match="Job stampa legacy già presente",
        ):
            materialize_resolved_legacy_events(
                database=db,
                materialized_at="2026-09-26T10:25:00+00:00",
            )

        assert db.connection.execute(
            "SELECT COUNT(*) FROM voucher_prints"
        ).fetchone()[0] == 0
        assert db.connection.execute(
            "SELECT materialized_at FROM legacy_audit_events"
        ).fetchone()[0] is None
    finally:
        db.close()


class _FakeEncryptedBackupService:
    def __init__(self, db, *, fail=False, encrypted=True):
        self.db = db
        self.fail = fail
        self.encrypted = encrypted
        self.calls = []

    def create(self, destination, *, password):
        self.calls.append(("create", str(destination), bool(password)))
        # The safety copy must happen before any migration row is committed.
        assert self.db.connection.execute(
            "SELECT COUNT(*) FROM migration_runs"
        ).fetchone()[0] == 0
        if self.fail:
            raise RuntimeError("synthetic backup failure")
        destination = Path(destination)
        destination.write_bytes(b"synthetic-protected-backup")
        return destination

    def is_encrypted_backup(self, source):
        self.calls.append(("verify-kind", str(source)))
        return self.encrypted

    def validate_encrypted(self, source, password):
        self.calls.append(("validate", str(source), bool(password)))
        if not self.encrypted:
            raise RuntimeError("synthetic unverified backup")
        return {"format": 2}

    def create_verified(self, destination, *, password):
        path = self.create(destination, password=password)
        encrypted = self.is_encrypted_backup(path)
        if not encrypted:
            return SimpleNamespace(
                path=path,
                sha256="b" * 64,
                backup_format=2,
                schema_version=2,
                encrypted=False,
            )
        manifest = self.validate_encrypted(path, password)
        return SimpleNamespace(
            path=path,
            sha256="b" * 64,
            backup_format=manifest["format"],
            schema_version=2,
            encrypted=True,
        )


def test_execute_requires_verified_encrypted_backup_before_migration(tmp_path):
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "event": "generate",
                "event_id": "execute-event",
                "voucher_id": _digest("12345-67890"),
                "timestamp": "2026-09-20T10:00:00+00:00",
            }
        ],
    )
    db, controller_id, _voucher_id = _database_with_voucher(tmp_path)
    try:
        plan = build_legacy_migration_plan(
            history_path=history,
            expected_fingerprint=FINGERPRINT,
            secret=FIXTURE_KEY,
            candidates=[
                LegacyVoucherCandidate(
                    controller_id,
                    "legacy-voucher-1",
                    "1234567890",
                )
            ],
        )
        service = _FakeEncryptedBackupService(db)
        backup = tmp_path / "pre-migration.vmbk"

        result = execute_legacy_migration(
            database=db,
            backup_service=service,
            backup_destination=backup,
            backup_password="fixture-migration-passphrase",
            plan=plan,
            migration_uuid="migration-execute",
            applied_at="2026-09-26T10:40:00+00:00",
            materialized_at="2026-09-26T10:41:00+00:00",
        )

        assert result.backup_path == backup
        assert backup.is_file()
        assert [call[0] for call in service.calls] == [
            "create",
            "verify-kind",
            "validate",
        ]
        run = db.connection.execute(
            """SELECT status, completed_at FROM migration_runs
               WHERE migration_uuid='migration-execute'"""
        ).fetchone()
        assert tuple(run) == (
            "COMPLETED",
            "2026-09-26T10:41:00+00:00",
        )
        assert db.connection.execute(
            "SELECT COUNT(*) FROM voucher_events"
        ).fetchone()[0] == 1
        backup_row = db.connection.execute(
            "SELECT * FROM backup_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert backup_row["status"] == "SUCCESS"
        assert backup_row["destination"] == "PRE_MIGRATION"
        assert backup_row["filename"] == "pre-migration.vmbk"
        assert backup_row["sha256"] == "b" * 64
        assert backup_row["backup_format"] == 2
        assert backup_row["schema_version"] == 2
        db.integrity_check()
    finally:
        db.close()


def test_execute_backup_failure_leaves_database_unmodified(tmp_path):
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "event": "generate",
                "event_id": "backup-failure-event",
                "voucher_id": _digest("12345-67890"),
                "timestamp": "2026-09-20T10:00:00+00:00",
            }
        ],
    )
    db, controller_id, _voucher_id = _database_with_voucher(tmp_path)
    try:
        plan = build_legacy_migration_plan(
            history_path=history,
            expected_fingerprint=FINGERPRINT,
            secret=FIXTURE_KEY,
            candidates=[
                LegacyVoucherCandidate(
                    controller_id,
                    "legacy-voucher-1",
                    "1234567890",
                )
            ],
        )
        service = _FakeEncryptedBackupService(db, fail=True)

        with pytest.raises(
            LegacyMigrationError,
            match="Backup di sicurezza",
        ):
            execute_legacy_migration(
                database=db,
                backup_service=service,
                backup_destination=tmp_path / "failed.vmbk",
                backup_password="fixture-migration-passphrase",
                plan=plan,
                migration_uuid="migration-backup-failure",
                applied_at="2026-09-26T10:40:00+00:00",
                materialized_at="2026-09-26T10:41:00+00:00",
            )

        assert db.connection.execute(
            "SELECT COUNT(*) FROM migration_runs"
        ).fetchone()[0] == 0
        assert db.connection.execute(
            "SELECT COUNT(*) FROM legacy_audit_events"
        ).fetchone()[0] == 0
        backup_row = db.connection.execute(
            "SELECT * FROM backup_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert backup_row["status"] == "FAILED"
        assert backup_row["destination"] == "PRE_MIGRATION"
        assert backup_row["sha256"] is None
        assert backup_row["error_summary"] == "RuntimeError"
    finally:
        db.close()


def test_execute_rejects_unverified_backup_before_database_mutation(tmp_path):
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "event": "generate",
                "event_id": "unencrypted-event",
                "voucher_id": _digest("12345-67890"),
                "timestamp": "2026-09-20T10:00:00+00:00",
            }
        ],
    )
    db, controller_id, _voucher_id = _database_with_voucher(tmp_path)
    try:
        plan = build_legacy_migration_plan(
            history_path=history,
            expected_fingerprint=FINGERPRINT,
            secret=FIXTURE_KEY,
            candidates=[
                LegacyVoucherCandidate(
                    controller_id,
                    "legacy-voucher-1",
                    "1234567890",
                )
            ],
        )
        service = _FakeEncryptedBackupService(db, encrypted=False)

        with pytest.raises(
            LegacyMigrationError,
            match="non verificabile",
        ):
            execute_legacy_migration(
                database=db,
                backup_service=service,
                backup_destination=tmp_path / "unsafe.vmbk",
                backup_password="fixture-migration-passphrase",
                plan=plan,
                migration_uuid="migration-unverified-backup",
                applied_at="2026-09-26T10:40:00+00:00",
                materialized_at="2026-09-26T10:41:00+00:00",
            )

        assert db.connection.execute(
            "SELECT COUNT(*) FROM migration_runs"
        ).fetchone()[0] == 0
        backup_row = db.connection.execute(
            "SELECT * FROM backup_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert backup_row["status"] == "FAILED"
        assert backup_row["error_summary"] == "UnverifiedBackup"
    finally:
        db.close()


def test_materialization_failure_keeps_evidence_retryable_not_operational(tmp_path):
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "event": "print",
                "voucher_id": _digest("12345-67890"),
                "timestamp": "2026-09-20T10:00:00+00:00",
                "output_file": "Voucher_Legacy.pdf",
                "document_copies": 1,
                "physical_copies": 1,
                "print_job_id": "execute-conflict",
            }
        ],
    )
    db, controller_id, _voucher_id = _database_with_voucher(tmp_path)
    try:
        plan = build_legacy_migration_plan(
            history_path=history,
            expected_fingerprint=FINGERPRINT,
            secret=FIXTURE_KEY,
            candidates=[
                LegacyVoucherCandidate(
                    controller_id,
                    "legacy-voucher-1",
                    "1234567890",
                )
            ],
        )
        db.connection.execute(
            """INSERT INTO print_jobs
               (print_job_uuid, created_at, submitted_at, windows_user,
                output_file, document_copies, status)
               VALUES ('execute-conflict', 't', 'different', 'operator',
                       'Other.pdf', 1, 'AUDITED')"""
        )
        db.connection.commit()
        service = _FakeEncryptedBackupService(db)

        with pytest.raises(
            LegacyMigrationError,
            match="Job stampa legacy già presente",
        ):
            execute_legacy_migration(
                database=db,
                backup_service=service,
                backup_destination=tmp_path / "retryable.vmbk",
                backup_password="fixture-migration-passphrase",
                plan=plan,
                migration_uuid="migration-materialize-failure",
                applied_at="2026-09-26T10:40:00+00:00",
                materialized_at="2026-09-26T10:41:00+00:00",
            )

        run = db.connection.execute(
            """SELECT status FROM migration_runs
               WHERE migration_uuid='migration-materialize-failure'"""
        ).fetchone()
        assert run["status"] == "EVIDENCE_READY"
        assert db.connection.execute(
            "SELECT COUNT(*) FROM legacy_audit_events"
        ).fetchone()[0] == 1
        assert db.connection.execute(
            "SELECT materialized_at FROM legacy_audit_events"
        ).fetchone()[0] is None
        assert db.connection.execute(
            "SELECT COUNT(*) FROM voucher_prints"
        ).fetchone()[0] == 0
    finally:
        db.close()
