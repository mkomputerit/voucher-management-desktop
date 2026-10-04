from voucher_management.alignment import (
    PRINT_STATE_NOT_PRINTED,
    PRINT_STATE_PRINTED,
    PRINT_STATE_UNKNOWN,
    align_vouchers,
    alignment_candidates,
)
from voucher_management.database import Database
from voucher_management.sync_store import persist_successful_snapshot
from voucher_management.unifi_api import ApiVoucher


def _voucher(remote_id: str, *, name: str = "Guest") -> ApiVoucher:
    return ApiVoucher(
        id=remote_id,
        code=f"CODE-{remote_id}",
        recipient=name,
        duration_minutes=60,
        create_time=1_700_000_000,
        quota=1,
        used=0,
        status="VALID_ONE",
        start_time=0,
        end_time=0,
    )


def _db(tmp_path):
    db = Database(tmp_path / "alignment.sqlite")
    db.initialize()
    controller = db.create_controller(
        name="Reception",
        api_root="https://controller.example",
        created_at="t",
    )
    return db, controller


def test_controller_import_requires_alignment(tmp_path):
    db, controller = _db(tmp_path)
    try:
        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[_voucher("external-1", name="Mario Rossi")],
            observed_at="2026-10-01T08:00:00+00:00",
            sync_uuid="discover",
        )

        candidates = alignment_candidates(db, controller_id=controller)

        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.name == "Mario Rossi"
        assert candidate.is_nominal is None
        assert candidate.print_state == PRINT_STATE_UNKNOWN
    finally:
        db.close()


def test_controller_discovered_voucher_aligns_unverified_print_as_not_printed(tmp_path):
    db, controller = _db(tmp_path)
    try:
        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[_voucher("external-2")],
            observed_at="2026-10-01T08:00:00+00:00",
            sync_uuid="discover",
        )
        voucher_id = int(
            db.connection.execute(
                "SELECT id FROM vouchers WHERE unifi_id='external-2'"
            ).fetchone()["id"]
        )

        for rejected_state in (PRINT_STATE_PRINTED, PRINT_STATE_UNKNOWN):
            try:
                align_vouchers(
                    db,
                    controller_id=controller,
                    voucher_ids=[voucher_id],
                    is_nominal=True,
                    print_state=rejected_state,
                    aligned_at="2026-10-01T09:00:00+00:00",
                    windows_user="PC\\operatore",
                )
            except ValueError:
                pass
            else:
                raise AssertionError(
                    "controller-discovered voucher without print evidence "
                    "must align as NOT_PRINTED"
                )

        result = align_vouchers(
            db,
            controller_id=controller,
            voucher_ids=[voucher_id],
            is_nominal=True,
            print_state=PRINT_STATE_NOT_PRINTED,
            aligned_at="2026-10-01T09:05:00+00:00",
            windows_user="PC\\operatore",
        )
        assert result.updated_ids == (voucher_id,)
        row = db.connection.execute(
            """SELECT is_nominal, print_state, alignment_completed_at
               FROM vouchers WHERE id=?""",
            (voucher_id,),
        ).fetchone()
        assert row["is_nominal"] == 1
        assert row["print_state"] == PRINT_STATE_NOT_PRINTED
        assert row["alignment_completed_at"] == "2026-10-01T09:05:00+00:00"
    finally:
        db.close()


def test_historical_unknown_origin_can_keep_unknown_print_state(tmp_path):
    db, controller = _db(tmp_path)
    try:
        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[_voucher("historical-unknown")],
            observed_at="2026-10-01T08:00:00+00:00",
            sync_uuid="discover",
        )
        voucher_id = int(
            db.connection.execute(
                "SELECT id FROM vouchers WHERE unifi_id='historical-unknown'"
            ).fetchone()["id"]
        )
        with db.transaction() as tx:
            tx.execute(
                "UPDATE vouchers SET origin='UNKNOWN' WHERE id=?",
                (voucher_id,),
            )

        align_vouchers(
            db,
            controller_id=controller,
            voucher_ids=[voucher_id],
            is_nominal=False,
            print_state=PRINT_STATE_UNKNOWN,
            aligned_at="2026-10-01T09:00:00+00:00",
            windows_user="PC\\operatore",
        )

        assert alignment_candidates(db, controller_id=controller) == ()
        row = db.connection.execute(
            """SELECT is_nominal, print_state, alignment_completed_at
               FROM vouchers WHERE id=?""",
            (voucher_id,),
        ).fetchone()
        assert row["is_nominal"] == 0
        assert row["print_state"] == PRINT_STATE_UNKNOWN
        assert row["alignment_completed_at"]
    finally:
        db.close()


def test_verified_print_history_cannot_be_downgraded_by_alignment(tmp_path):
    db, controller = _db(tmp_path)
    try:
        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[_voucher("external-verified-print")],
            observed_at="2026-10-01T08:00:00+00:00",
            sync_uuid="discover-verified-print",
        )
        voucher_id = int(
            db.connection.execute(
                "SELECT id FROM vouchers WHERE unifi_id='external-verified-print'"
            ).fetchone()["id"]
        )
        db.record_print_audit(
            controller_id=controller,
            audit_id="verified-alignment-print",
            codes=["CODE-external-verified-print"],
            output_file="legacy.pdf",
            document_copies=1,
            printed_at="2026-09-20T08:00:00+00:00",
            windows_user="MIGRATION",
        )

        try:
            align_vouchers(
                db,
                controller_id=controller,
                voucher_ids=[voucher_id],
                is_nominal=True,
                print_state="NOT_PRINTED",
                aligned_at="2026-10-01T09:00:00+00:00",
                windows_user="PC\\operatore",
            )
        except ValueError as exc:
            assert "stampa verificata" in str(exc)
        else:
            raise AssertionError("verified print evidence must not be downgraded")

        row = db.connection.execute(
            """SELECT print_state, alignment_completed_at
               FROM vouchers WHERE id=?""",
            (voucher_id,),
        ).fetchone()
        assert row["print_state"] == PRINT_STATE_PRINTED
        assert row["alignment_completed_at"] is None
    finally:
        db.close()


def test_positive_legacy_print_state_without_job_cannot_be_downgraded(tmp_path):
    db, controller = _db(tmp_path)
    try:
        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[_voucher("legacy-positive-print")],
            observed_at="2026-10-01T08:00:00+00:00",
            sync_uuid="discover-legacy-positive-print",
        )
        voucher_id = int(
            db.connection.execute(
                "SELECT id FROM vouchers WHERE unifi_id='legacy-positive-print'"
            ).fetchone()["id"]
        )
        with db.transaction() as tx:
            tx.execute(
                """UPDATE vouchers
                   SET origin='UNKNOWN',
                       print_state='PRINTED',
                       alignment_completed_at=NULL
                   WHERE id=?""",
                (voucher_id,),
            )

        candidate = alignment_candidates(
            db,
            controller_id=controller,
        )[0]
        assert candidate.print_state == PRINT_STATE_PRINTED
        assert candidate.last_printed_at == ""

        try:
            align_vouchers(
                db,
                controller_id=controller,
                voucher_ids=[voucher_id],
                is_nominal=False,
                print_state="NOT_PRINTED",
                aligned_at="2026-10-01T09:00:00+00:00",
                windows_user=r"PC\operatore",
            )
        except ValueError as exc:
            assert "prova positiva" in str(exc)
        else:
            raise AssertionError(
                "positive legacy PRINTED state must not be downgraded"
            )

        row = db.connection.execute(
            """SELECT print_state, alignment_completed_at
               FROM vouchers WHERE id=?""",
            (voucher_id,),
        ).fetchone()
        assert row["print_state"] == PRINT_STATE_PRINTED
        assert row["alignment_completed_at"] is None
    finally:
        db.close()


def test_alignment_is_atomic_for_mixed_controller_selection(tmp_path):
    db, controller = _db(tmp_path)
    other = db.create_controller(
        name="Other",
        api_root="https://other.example",
        created_at="t",
    )
    try:
        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[_voucher("a")],
            observed_at="2026-10-01T08:00:00+00:00",
            sync_uuid="a",
        )
        persist_successful_snapshot(
            db,
            controller_id=other,
            vouchers=[_voucher("b")],
            observed_at="2026-10-01T08:00:00+00:00",
            sync_uuid="b",
        )
        rows = db.connection.execute(
            "SELECT id, controller_id FROM vouchers ORDER BY id"
        ).fetchall()

        try:
            align_vouchers(
                db,
                controller_id=controller,
                voucher_ids=[int(rows[0]["id"]), int(rows[1]["id"])],
                is_nominal=True,
                print_state=PRINT_STATE_PRINTED,
                aligned_at="2026-10-01T09:00:00+00:00",
                windows_user="PC\\operatore",
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("mixed-controller alignment must fail")

        assert db.connection.execute(
            "SELECT COUNT(*) FROM vouchers WHERE alignment_completed_at IS NOT NULL"
        ).fetchone()[0] == 0
    finally:
        db.close()
