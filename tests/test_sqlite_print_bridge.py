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
