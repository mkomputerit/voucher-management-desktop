"""UI-independent duplicate-print policy for Voucher Management 5.0."""

from __future__ import annotations

from dataclasses import dataclass

from .database import PrintAuditSummary


@dataclass(frozen=True)
class ReprintWarning:
    """Facts the UI must show before producing a duplicate voucher."""

    required: bool
    previous_print_jobs: int = 0
    previous_physical_copies: int = 0
    first_printed_at: str = ""
    last_printed_at: str = ""
    print_history_incomplete: bool = False


def evaluate_reprint(
    summary: PrintAuditSummary,
    *,
    ever_used: bool | None = None,
) -> ReprintWarning:
    """Require confirmation only when duplicate-print evidence is meaningful.

    A verified physical print always requires confirmation. Legacy PRINTED
    state without an auditable print job is weaker evidence: if UniFi has
    positively observed no use, Voucher Management allows an operational print;
    if the voucher was used, or usage evidence is indeterminate, it remains a
    conservative duplicate warning.
    """

    if summary.print_jobs <= 0:
        if not summary.known_printed_without_audit:
            return ReprintWarning(required=False)
        if ever_used is False:
            return ReprintWarning(required=False)
        return ReprintWarning(
            required=True,
            print_history_incomplete=True,
        )
    return ReprintWarning(
        required=True,
        previous_print_jobs=summary.print_jobs,
        previous_physical_copies=summary.physical_copies,
        first_printed_at=summary.first_printed_at,
        last_printed_at=summary.last_printed_at,
    )
