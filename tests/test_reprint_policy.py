"""Tests for the duplicate physical-print warning policy."""

from voucher_management.database import PrintAuditSummary
from voucher_management.reprint_policy import evaluate_reprint


def test_first_physical_print_needs_no_duplicate_warning():
    warning = evaluate_reprint(PrintAuditSummary(0, 0, "", ""))
    assert warning.required is False


def test_any_previous_physical_print_requires_warning():
    warning = evaluate_reprint(
        PrintAuditSummary(
            print_jobs=2,
            physical_copies=3,
            first_printed_at="2026-09-25T10:00:00Z",
            last_printed_at="2026-09-25T11:00:00Z",
        )
    )
    assert warning.required is True
    assert warning.previous_print_jobs == 2
    assert warning.previous_physical_copies == 3
    assert warning.last_printed_at.endswith("11:00:00Z")
