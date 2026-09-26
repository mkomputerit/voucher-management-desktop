"""Privacy policy for voucher report fields.

Report generation must ask this module whether a clear voucher code may leave
the operational UI. Keeping the decision centralized prevents a new PDF/CSV
export from exposing reusable credentials by accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ReportPurpose(str, Enum):
    """Supported report purposes with distinct credential exposure needs."""

    SUMMARY = "summary"
    AUDIT = "audit"
    OPERATIONAL_HANDOFF = "operational_handoff"


@dataclass(frozen=True)
class ReportCodePolicy:
    """Decision returned to every report renderer."""

    expose_code: bool
    reason: str


def voucher_code_policy(
    purpose: ReportPurpose,
    *,
    include_code_requested: bool = False,
) -> ReportCodePolicy:
    """Return whether a clear voucher code may be included in a report.

    Summary and audit reports are historical/administrative outputs and never
    need a reusable credential. Operational handoff is the only report purpose
    allowed to expose codes, and only after an explicit operator request.
    """

    if purpose is ReportPurpose.OPERATIONAL_HANDOFF and include_code_requested:
        return ReportCodePolicy(True, "explicit_operational_handoff")

    if include_code_requested:
        return ReportCodePolicy(False, "code_not_required_for_report_purpose")

    return ReportCodePolicy(False, "code_hidden_by_default")


def report_code_value(
    code: str,
    purpose: ReportPurpose,
    *,
    include_code_requested: bool = False,
) -> str:
    """Return the report-safe code value; empty means omit the field/value."""

    decision = voucher_code_policy(
        purpose,
        include_code_requested=include_code_requested,
    )
    return code if decision.expose_code else ""
