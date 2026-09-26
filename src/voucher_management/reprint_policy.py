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


def evaluate_reprint(summary: PrintAuditSummary) -> ReprintWarning:
    """Require explicit confirmation after any prior physical print.

    PDF generation alone is intentionally not a reprint boundary. Only a
    recorded physical-print audit requires the duplicate warning.
    """

    if summary.print_jobs <= 0:
        return ReprintWarning(required=False)
    return ReprintWarning(
        required=True,
        previous_print_jobs=summary.print_jobs,
        previous_physical_copies=summary.physical_copies,
        first_printed_at=summary.first_printed_at,
        last_printed_at=summary.last_printed_at,
    )
