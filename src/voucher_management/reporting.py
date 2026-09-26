"""Privacy-safe reporting models built from durable SQLite facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Iterable

from .database import Database
from .report_policy import ReportPurpose, report_code_value


class ReportKind(str, Enum):
    """Operator-facing report views over the same durable facts."""

    SUMMARY = "summary"
    USED = "used"
    EXPIRED = "expired"
    PRINTED_UNUSED = "printed_unused"
    NEVER_PRINTED = "never_printed"
    NOMINAL = "nominal"
    FULL_HISTORY = "full_history"


REPORT_TITLES = {
    ReportKind.SUMMARY: "Riepilogo voucher",
    ReportKind.USED: "Voucher utilizzati",
    ReportKind.EXPIRED: "Voucher scaduti",
    ReportKind.PRINTED_UNUSED: "Voucher stampati mai utilizzati",
    ReportKind.NEVER_PRINTED: "Voucher mai stampati",
    ReportKind.NOMINAL: "Voucher nominali",
    ReportKind.FULL_HISTORY: "Storico completo voucher",
}


@dataclass(frozen=True)
class ReportTotals:
    """Aggregates calculated from atomic voucher and print facts."""

    vouchers: int
    used_vouchers: int
    total_controller_uses: int
    expired_vouchers: int
    printed_vouchers: int
    print_jobs: int
    physical_copies: int
    reprint_jobs: int
    reprint_copies: int
    printed_never_used: int
    never_printed: int
    nominal_vouchers: int


@dataclass(frozen=True)
class ReportRow:
    """One report-safe voucher row.

    The code field is already filtered through report_policy before the row
    leaves the reporting boundary. Summary/audit renderers therefore never
    receive the raw reusable credential.
    """

    voucher_id: int
    controller_name: str
    code: str
    recipient: str
    assigned_to: str
    created_at: str
    imported_at: str
    expires_at: str
    authorized_guest_count: int
    print_jobs: int
    physical_copies: int
    reprint_jobs: int
    reprint_copies: int
    first_printed_at: str
    last_printed_at: str
    print_operators: tuple[str, ...]
    expired: bool
    present_on_controller: bool
    status: str


@dataclass(frozen=True)
class ReportDataset:
    """Complete renderer input with privacy policy already applied."""

    kind: ReportKind
    purpose: ReportPurpose
    title: str
    generated_at: str
    controller_label: str
    rows: tuple[ReportRow, ...]
    totals: ReportTotals
    code_exposed: bool


def _purpose_for_kind(kind: ReportKind) -> ReportPurpose:
    return (
        ReportPurpose.AUDIT
        if kind is ReportKind.FULL_HISTORY
        else ReportPurpose.SUMMARY
    )


def _operators(value: object) -> tuple[str, ...]:
    values = {
        item.strip()
        for item in str(value or "").split(",")
        if item.strip()
    }
    return tuple(sorted(values, key=str.casefold))


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _expired_at_report_time(
    *,
    persisted_expired: bool,
    expires_at: object,
    generated_at: str,
) -> bool:
    if persisted_expired:
        return True
    expiry = _parse_time(expires_at)
    report_time = _parse_time(generated_at)
    if expiry is None or report_time is None:
        return False
    if expiry.tzinfo is None and report_time.tzinfo is not None:
        expiry = expiry.replace(tzinfo=report_time.tzinfo)
    if report_time.tzinfo is None and expiry.tzinfo is not None:
        report_time = report_time.replace(tzinfo=expiry.tzinfo)
    try:
        return expiry <= report_time
    except TypeError:
        return False


def _status(*, expired: bool, uses: int, print_jobs: int) -> str:
    if expired:
        return "Scaduto"
    if uses > 0:
        return "Utilizzato"
    if print_jobs > 0:
        return "Stampato"
    return "Mai stampato"


def _matches(kind: ReportKind, row: ReportRow) -> bool:
    if kind in {ReportKind.SUMMARY, ReportKind.FULL_HISTORY}:
        return True
    if kind is ReportKind.USED:
        return row.authorized_guest_count > 0
    if kind is ReportKind.EXPIRED:
        return row.expired
    if kind is ReportKind.PRINTED_UNUSED:
        return row.print_jobs > 0 and row.authorized_guest_count == 0
    if kind is ReportKind.NEVER_PRINTED:
        return row.print_jobs == 0
    if kind is ReportKind.NOMINAL:
        return bool(row.assigned_to.strip())
    raise ValueError(f"Unsupported report kind: {kind}")


def _totals(rows: Iterable[ReportRow]) -> ReportTotals:
    materialized = tuple(rows)
    return ReportTotals(
        vouchers=len(materialized),
        used_vouchers=sum(row.authorized_guest_count > 0 for row in materialized),
        total_controller_uses=sum(
            row.authorized_guest_count for row in materialized
        ),
        expired_vouchers=sum(row.expired for row in materialized),
        printed_vouchers=sum(row.print_jobs > 0 for row in materialized),
        print_jobs=sum(row.print_jobs for row in materialized),
        physical_copies=sum(row.physical_copies for row in materialized),
        reprint_jobs=sum(row.reprint_jobs for row in materialized),
        reprint_copies=sum(row.reprint_copies for row in materialized),
        printed_never_used=sum(
            row.print_jobs > 0 and row.authorized_guest_count == 0
            for row in materialized
        ),
        never_printed=sum(row.print_jobs == 0 for row in materialized),
        nominal_vouchers=sum(bool(row.assigned_to.strip()) for row in materialized),
    )


def build_report_dataset(
    database: Database,
    *,
    kind: ReportKind,
    generated_at: str,
    controller_id: int | None = None,
    include_code_requested: bool = False,
) -> ReportDataset:
    """Build one report without inventing facts absent from SQLite.

    Controller usage counters are current/last-observed totals. Their value is
    never presented as an exact use timestamp. Print facts come only from the
    local physical-print audit.
    """

    purpose = _purpose_for_kind(kind)
    raw_rows = database.report_voucher_rows(controller_id=controller_id)

    rows: list[ReportRow] = []
    controllers: set[str] = set()
    code_exposed = False
    for raw in raw_rows:
        controller_name = str(raw["controller_name"] or "").strip() or "Controller"
        controllers.add(controller_name)
        clear_code = report_code_value(
            str(raw["code"] or ""),
            purpose,
            include_code_requested=include_code_requested,
        )
        if clear_code:
            code_exposed = True

        print_jobs = int(raw["print_jobs"] or 0)
        uses = int(raw["authorized_guest_count"] or 0)
        expired = _expired_at_report_time(
            persisted_expired=bool(raw["expired"]),
            expires_at=raw["expires_at"],
            generated_at=generated_at,
        )
        assigned_to = str(raw["assigned_to"] or "").strip()
        recipient = assigned_to or str(raw["name"] or "").strip()
        row = ReportRow(
            voucher_id=int(raw["voucher_id"]),
            controller_name=controller_name,
            code=clear_code,
            recipient=recipient,
            assigned_to=assigned_to,
            created_at=str(raw["created_at"] or ""),
            imported_at=str(raw["imported_at"] or ""),
            expires_at=str(raw["expires_at"] or ""),
            authorized_guest_count=uses,
            print_jobs=print_jobs,
            physical_copies=int(raw["physical_copies"] or 0),
            reprint_jobs=int(raw["reprint_jobs"] or 0),
            reprint_copies=int(raw["reprint_copies"] or 0),
            first_printed_at=str(raw["first_printed_at"] or ""),
            last_printed_at=str(raw["last_printed_at"] or ""),
            print_operators=_operators(raw["print_operators"]),
            expired=expired,
            present_on_controller=bool(raw["present_on_controller"]),
            status=_status(
                expired=expired,
                uses=uses,
                print_jobs=print_jobs,
            ),
        )
        if _matches(kind, row):
            rows.append(row)

    if controller_id is None:
        controller_label = "Tutti i controller"
    else:
        controller_label = (
            database.controller_name(controller_id)
            or (
                next(iter(controllers))
                if len(controllers) == 1
                else "Controller selezionato"
            )
        )

    materialized = tuple(rows)
    return ReportDataset(
        kind=kind,
        purpose=purpose,
        title=REPORT_TITLES[kind],
        generated_at=str(generated_at),
        controller_label=controller_label,
        rows=materialized,
        totals=_totals(materialized),
        code_exposed=code_exposed,
    )
