"""Tests for the Milestone A physical-print to SQLite bridge."""

from pathlib import Path
from types import SimpleNamespace

from voucher_management.app import VoucherApp


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
        Path(r"C:\Print\Voucher_Test.pdf"),
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
