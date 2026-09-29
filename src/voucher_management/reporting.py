"""Historical reporting models built from durable SQLite facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Iterable

from .database import Database
from .report_policy import ReportPurpose, report_code_value


class ReportKind(str, Enum):
    """Operator-facing historical views over the local archive."""

    SUMMARY = "summary"
    GENERATED = "generated"
    GENERATED_UNUSED = "generated_unused"
    USED = "used"
    EXPIRED = "expired"
    PRINTED = "printed"
    PRINTED_UNUSED = "printed_unused"
    NEVER_PRINTED = "never_printed"
    NOMINAL = "nominal"
    FULL_HISTORY = "full_history"


REPORT_TITLES = {
    ReportKind.SUMMARY: "Riepilogo storico voucher",
    ReportKind.GENERATED: "Voucher generati da Voucher Management",
    ReportKind.GENERATED_UNUSED: "Voucher generati e mai utilizzati",
    ReportKind.USED: "Voucher utilizzati",
    ReportKind.EXPIRED: "Voucher scaduti",
    ReportKind.PRINTED: "Voucher stampati",
    ReportKind.PRINTED_UNUSED: "Voucher stampati mai utilizzati",
    ReportKind.NEVER_PRINTED: "Voucher mai stampati",
    ReportKind.NOMINAL: "Voucher nominali",
    ReportKind.FULL_HISTORY: "Storico completo voucher",
}


@dataclass(frozen=True)
class ReportTotals:
    """Aggregates calculated from durable local voucher and print facts."""

    vouchers: int
    generated_by_app: int
    generated_never_used: int
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
    non_nominal_vouchers: int
    unclassified_nominality: int


@dataclass(frozen=True)
class ReportRow:
    """One privacy-safe historical voucher row."""

    voucher_id: int
    controller_name: str
    code: str
    recipient: str
    assigned_to: str
    origin: str | None
    is_nominal: bool | None
    classification_updated_at: str
    ever_used: bool
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
    archived_at: str
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


def _nullable_bool(value: object) -> bool | None:
    if value is None:
        return None
    return bool(value)


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


def _status(
    *,
    archived: bool,
    expired: bool,
    ever_used: bool,
    print_jobs: int,
) -> str:
    if archived:
        return "Archiviato"
    if expired:
        return "Scaduto"
    if ever_used:
        return "Utilizzato"
    if print_jobs > 0:
        return "Stampato"
    return "Mai stampato"


def _matches(kind: ReportKind, row: ReportRow) -> bool:
    if kind in {ReportKind.SUMMARY, ReportKind.FULL_HISTORY}:
        return True
    if kind is ReportKind.GENERATED:
        return row.origin == "APPLICATION"
    if kind is ReportKind.GENERATED_UNUSED:
        return row.origin == "APPLICATION" and not row.ever_used
    if kind is ReportKind.USED:
        return row.ever_used
    if kind is ReportKind.EXPIRED:
        return row.expired
    if kind is ReportKind.PRINTED:
        return row.print_jobs > 0
    if kind is ReportKind.PRINTED_UNUSED:
        return row.print_jobs > 0 and not row.ever_used
    if kind is ReportKind.NEVER_PRINTED:
        return row.print_jobs == 0
    if kind is ReportKind.NOMINAL:
        return row.is_nominal is True
    raise ValueError(f"Unsupported report kind: {kind}")


def _totals(rows: Iterable[ReportRow]) -> ReportTotals:
    materialized = tuple(rows)
    return ReportTotals(
        vouchers=len(materialized),
        generated_by_app=sum(row.origin == "APPLICATION" for row in materialized),
        generated_never_used=sum(
            row.origin == "APPLICATION" and not row.ever_used
            for row in materialized
        ),
        used_vouchers=sum(row.ever_used for row in materialized),
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
            row.print_jobs > 0 and not row.ever_used
            for row in materialized
        ),
        never_printed=sum(row.print_jobs == 0 for row in materialized),
        nominal_vouchers=sum(row.is_nominal is True for row in materialized),
        non_nominal_vouchers=sum(row.is_nominal is False for row in materialized),
        unclassified_nominality=sum(
            row.is_nominal is None for row in materialized
        ),
    )


def build_report_dataset(
    database: Database,
    *,
    kind: ReportKind,
    generated_at: str,
    controller_id: int | None = None,
    include_code_requested: bool = False,
) -> ReportDataset:
    """Build one historical report only from facts persisted locally.

    ever_used is sticky local evidence: once a synchronization observes a
    voucher with a positive controller usage count, later snapshots cannot
    make it "never used" again. Voucher nominality and application creation
    origin are application-owned facts and are never inferred from recipient
    text.

    Controller usage counters remain the latest observed values and are never
    presented as exact use timestamps. Physical-print facts come only from the
    local print audit.
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
        ever_used = bool(raw["ever_used"]) or uses > 0
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
            origin=(
                str(raw["origin"]).strip().upper()
                if raw["origin"] is not None
                else None
            ),
            is_nominal=_nullable_bool(raw["is_nominal"]),
            classification_updated_at=str(
                raw["classification_updated_at"] or ""
            ),
            ever_used=ever_used,
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
            archived_at=str(raw["archived_at"] or ""),
            status=_status(
                archived=bool(raw["archived_at"]),
                expired=expired,
                ever_used=ever_used,
                print_jobs=print_jobs,
            ),
        )
        if _matches(kind, row):
            rows.append(row)

    if controller_id is None:
        controller_label = "Tutto lo storico locale"
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
