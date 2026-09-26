"""Tests for the Milestone A physical-print to SQLite bridge."""

from pathlib import Path
from types import SimpleNamespace

from voucher_management.app import VoucherApp
from voucher_management.database import PrintAuditSummary


class _DatabaseRecorder:
    def __init__(self):
        self.calls = []

    def record_print_audit(self, **kwargs):
        self.calls.append(kwargs)


def test_confirmed_print_is_mirrored_with_stable_audit_identity():
    database = _DatabaseRecorder()
    fake = SimpleNamespace(
        active_controller_id=7,
        database=database,
        _windows_operator_identity=lambda: r"SALA\operatore",
    )
    pending = {
        "audit_id": "abc123",
        "copies": 2,
        "submitted_at": "2026-09-26T09:00:00+00:00",
    }

    VoucherApp._record_sqlite_print_audit(
        fake,
        pending,
        ["12345-67890", "12345-67890"],
        Path("Voucher_Test.pdf"),
    )

    assert database.calls == [
        {
            "controller_id": 7,
            "audit_id": "abc123",
            "codes": ["12345-67890", "12345-67890"],
            "output_file": "Voucher_Test.pdf",
            "document_copies": 2,
            "printed_at": "2026-09-26T09:00:00+00:00",
            "windows_user": r"SALA\operatore",
        }
    ]


def test_confirmed_print_deselects_only_printed_vouchers():
    refreshed = []
    fake = SimpleNamespace(
        checked_ids={"v1", "v2", "v3"},
        vouchers=[
            SimpleNamespace(id="v1", code_formatted="11111-22222"),
            SimpleNamespace(id="v2", code_formatted="33333-44444"),
            SimpleNamespace(id="v3", code_formatted="55555-66666"),
        ],
        populate=lambda: refreshed.append(True),
    )

    VoucherApp._deselect_printed_codes(
        fake,
        ["1111122222", "55555-66666"],
    )

    assert fake.checked_ids == {"v2"}
    assert refreshed == [True]


def test_sqlite_print_bridge_fails_closed_without_controller():
    fake = SimpleNamespace(
        active_controller_id=None,
        database=_DatabaseRecorder(),
    )

    try:
        VoucherApp._record_sqlite_print_audit(
            fake,
            {
                "audit_id": "abc123",
                "copies": 1,
                "submitted_at": "2026-09-26T09:00:00+00:00",
            },
            ["12345-67890"],
            Path("Voucher_Test.pdf"),
        )
    except RuntimeError as exc:
        assert "Controller locale" in str(exc)
    else:
        raise AssertionError("missing controller must block SQLite print audit")


def test_pending_print_recovery_commits_sqlite_before_marker_finalize():
    calls = []

    class History:
        def resolve_pending_print(self, candidate_codes, settings):
            calls.append(("resolve", tuple(candidate_codes), dict(settings)))
            return SimpleNamespace(
                state="submitted",
                audit_id="recover-1",
                codes=("12345-67890",),
                output_file="Voucher_Recover.pdf",
                document_copies=2,
                submitted_at="2026-09-26T09:45:00+00:00",
            )

        def finalize_pending_print_audit(self, audit_id):
            calls.append(("finalize", audit_id))

    fake = SimpleNamespace(
        history=History(),
        vouchers=[SimpleNamespace(code_formatted="12345-67890")],
        settings={"structure_name": "Test"},
        _record_sqlite_print_audit=lambda pending, codes, path: calls.append(
            ("sqlite", dict(pending), tuple(codes), path.name)
        ),
    )

    assert VoucherApp._record_pending_print_sqlite_and_finalize(fake) is True
    assert calls[0][0] == "resolve"
    assert calls[1][0] == "sqlite"
    assert calls[2] == ("finalize", "recover-1")


def test_reprint_preflight_allows_first_physical_print(monkeypatch):
    class Database:
        def print_summaries_for_codes(self, *, controller_id, codes):
            assert controller_id == 7
            return {
                "1234567890": PrintAuditSummary(0, 0, "", ""),
            }

    monkeypatch.setattr(
        "voucher_management.app.ReprintConfirmDialog",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("first print must not show a reprint dialog")
        ),
    )
    fake = SimpleNamespace(
        active_controller_id=7,
        database=Database(),
        logger=SimpleNamespace(warning=lambda *args, **kwargs: None),
    )

    assert VoucherApp._confirm_physical_reprint(
        fake,
        ["12345-67890"],
        object(),
    ) is True


def test_reprint_preflight_collects_only_previously_printed_vouchers(monkeypatch):
    captured = {}

    class Database:
        def print_summaries_for_codes(self, *, controller_id, codes):
            return {
                "1111122222": PrintAuditSummary(
                    2, 3, "2026-09-26T08:00:00+00:00", "2026-09-26T09:00:00+00:00"
                ),
                "3333344444": PrintAuditSummary(0, 0, "", ""),
                "5555566666": PrintAuditSummary(
                    1, 1, "2026-09-26T09:15:00+00:00", "2026-09-26T09:15:00+00:00"
                ),
            }

    class Dialog:
        def __init__(self, parent, warnings):
            captured["warnings"] = warnings
            self.result = True

    monkeypatch.setattr("voucher_management.app.ReprintConfirmDialog", Dialog)
    fake = SimpleNamespace(
        active_controller_id=7,
        database=Database(),
        logger=SimpleNamespace(warning=lambda *args, **kwargs: None),
    )

    assert VoucherApp._confirm_physical_reprint(
        fake,
        ["11111-22222", "33333-44444", "55555-66666"],
        object(),
    ) is True
    assert [code for code, _warning in captured["warnings"]] == [
        "11111-22222",
        "55555-66666",
    ]
    assert captured["warnings"][0][1].previous_print_jobs == 2
