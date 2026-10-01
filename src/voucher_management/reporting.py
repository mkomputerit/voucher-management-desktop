"""Privacy-safe reporting models built from durable SQLite facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterable

from .database import Database
from .report_policy import ReportPurpose, report_code_value


class ReportKind(str, Enum):
    """Operator-facing historical views over durable local facts."""

    SUMMARY = "summary"
    GENERATED = "generated"
    GENERATED_UNUSED = "generated_unused"
    USED = "used"
    EXPIRED = "expired"
    PRINTED = "printed"
    PRINTED_UNUSED = "printed_unused"
    NEVER_PRINTED = "never_printed"
    NOMINAL = "nominal"
    NON_NOMINAL = "non_nominal"
    UNCLASSIFIED = "unclassified"
    USAGE_UNKNOWN = "usage_unknown"
    ORIGIN_UNKNOWN = "origin_unknown"
    NOMINALITY_REDACTED = "nominality_redacted"
    SECURITY_REVOKED = "security_revoked"
    FULL_HISTORY = "full_history"


REPORT_TITLES = {
    ReportKind.SUMMARY: "Riepilogo storico voucher",
    ReportKind.GENERATED: "Creazione Voucher Management confermata",
    ReportKind.GENERATED_UNUSED: "Creazione VM confermata e mai osservata utilizzata",
    ReportKind.USED: "Voucher utilizzati",
    ReportKind.EXPIRED: "Voucher scaduti",
    ReportKind.PRINTED: "Voucher stampati",
    ReportKind.PRINTED_UNUSED: "Voucher stampati senza uso positivo osservato",
    ReportKind.NEVER_PRINTED: "Voucher mai stampati",
    ReportKind.NOMINAL: "Voucher nominali",
    ReportKind.NON_NOMINAL: "Voucher non nominali",
    ReportKind.UNCLASSIFIED: "Voucher non classificati",
    ReportKind.USAGE_UNKNOWN: "Voucher con utilizzo non determinabile",
    ReportKind.ORIGIN_UNKNOWN: "Voucher con origine creazione non determinabile",
    ReportKind.NOMINALITY_REDACTED: "Nominalità rimossa per privacy",
    ReportKind.SECURITY_REVOKED: "Voucher revocati per sicurezza",
    ReportKind.FULL_HISTORY: "Storico completo voucher",
}


APPLICATION_ORIGINS = frozenset({"APPLICATION"})


@dataclass(frozen=True)
class ReportTotals:
    """Aggregates calculated only from rows represented by this dataset."""

    vouchers: int
    generated_vouchers: int
    used_vouchers: int
    never_used_vouchers: int
    usage_unknown_vouchers: int
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
    unclassified_vouchers: int
    unknown_origin_vouchers: int = 0
    redacted_nominality_vouchers: int = 0
    security_revoked_vouchers: int = 0
    printed_usage_unknown: int = 0


@dataclass(frozen=True)
class ReportRow:
    """One report-safe durable voucher row."""

    voucher_id: int
    controller_name: str
    code: str
    recipient: str
    created_at: str
    imported_at: str
    expires_at: str
    authorized_guest_count: int
    ever_used: bool
    usage_observed: bool
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
    origin: str
    is_nominal: bool | None
    last_synced_at: str = ""
    nominality_redacted: bool = False
    security_revoked_at: str = ""


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
    data_from: str = ""
    data_as_of: str = ""


def _purpose_for_kind(kind: ReportKind) -> ReportPurpose:
    return ReportPurpose.AUDIT if kind is ReportKind.FULL_HISTORY else ReportPurpose.SUMMARY


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


def _status(
    *,
    archived: bool,
    expired: bool,
    ever_used: bool,
    print_jobs: int,
    security_revoked: bool = False,
) -> str:
    if security_revoked:
        return "Revocato per sicurezza"
    if archived:
        return "Archiviato"
    if expired:
        return "Scaduto"
    if ever_used:
        return "Utilizzato"
    if print_jobs > 0:
        return "Stampato"
    return "Mai stampato"


def origin_label(origin: str) -> str:
    return {
        "APPLICATION": "Voucher Management",
        "LEGACY_APPLICATION": "Evidenza legacy (creazione non provata)",
        "CONTROLLER": "Rilevato sulla controller (creatore non provato)",
        "UNKNOWN": "Non determinata",
    }.get(str(origin or "").strip(), "Non determinata")


def nominal_label(value: bool | None, *, redacted: bool = False) -> str:
    if redacted:
        return "Rimossa per privacy"
    if value is True:
        return "Sì"
    if value is False:
        return "No"
    return "Non classificato"


def _matches(kind: ReportKind, row: ReportRow) -> bool:
    if kind in {ReportKind.SUMMARY, ReportKind.FULL_HISTORY}:
        return True
    if kind is ReportKind.GENERATED:
        return row.origin in APPLICATION_ORIGINS
    if kind is ReportKind.GENERATED_UNUSED:
        return (
            row.origin in APPLICATION_ORIGINS
            and row.usage_observed
            and not row.ever_used
        )
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
    if kind is ReportKind.NON_NOMINAL:
        return row.is_nominal is False
    if kind is ReportKind.UNCLASSIFIED:
        return row.is_nominal is None and not row.nominality_redacted
    if kind is ReportKind.USAGE_UNKNOWN:
        return not row.usage_observed
    if kind is ReportKind.ORIGIN_UNKNOWN:
        return row.origin != "APPLICATION"
    if kind is ReportKind.NOMINALITY_REDACTED:
        return row.nominality_redacted
    if kind is ReportKind.SECURITY_REVOKED:
        return bool(row.security_revoked_at)
    raise ValueError(f"Unsupported report kind: {kind}")


def _totals(rows: Iterable[ReportRow]) -> ReportTotals:
    materialized = tuple(rows)
    return ReportTotals(
        vouchers=len(materialized),
        generated_vouchers=sum(row.origin in APPLICATION_ORIGINS for row in materialized),
        used_vouchers=sum(row.ever_used for row in materialized),
        never_used_vouchers=sum(
            row.usage_observed and not row.ever_used for row in materialized
        ),
        usage_unknown_vouchers=sum(
            not row.usage_observed for row in materialized
        ),
        total_controller_uses=sum(row.authorized_guest_count for row in materialized),
        expired_vouchers=sum(row.expired for row in materialized),
        printed_vouchers=sum(row.print_jobs > 0 for row in materialized),
        print_jobs=sum(row.print_jobs for row in materialized),
        physical_copies=sum(row.physical_copies for row in materialized),
        reprint_jobs=sum(row.reprint_jobs for row in materialized),
        reprint_copies=sum(row.reprint_copies for row in materialized),
        printed_never_used=sum(
            row.print_jobs > 0 and row.usage_observed and not row.ever_used
            for row in materialized
        ),
        never_printed=sum(row.print_jobs == 0 for row in materialized),
        nominal_vouchers=sum(row.is_nominal is True for row in materialized),
        non_nominal_vouchers=sum(row.is_nominal is False for row in materialized),
        unclassified_vouchers=sum(
            row.is_nominal is None and not row.nominality_redacted
            for row in materialized
        ),
        unknown_origin_vouchers=sum(
            row.origin != "APPLICATION" for row in materialized
        ),
        redacted_nominality_vouchers=sum(
            row.nominality_redacted for row in materialized
        ),
        security_revoked_vouchers=sum(
            bool(row.security_revoked_at) for row in materialized
        ),
        printed_usage_unknown=sum(
            row.print_jobs > 0 and not row.usage_observed
            for row in materialized
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
    """Build historical reports from facts Voucher Management actually retained.

    "Used" means at least one positive authorized-guest count was observed in
    the retained controller history. Negative views mean only "never observed
    used" through the row's last controller observation; rows without controller
    usage evidence stay explicitly
    indeterminate. Print facts come exclusively from the local physical-print
    audit. Nominality and creation provenance are application-owned classifications;
    they are never inferred from a recipient string.
    """

    purpose = _purpose_for_kind(kind)
    raw_rows = database.report_voucher_rows(controller_id=controller_id)

    rows: list[ReportRow] = []
    controllers: set[str] = set()
    code_exposed = False
    seen_voucher_ids: set[int] = set()
    for raw in raw_rows:
        voucher_id = int(raw["voucher_id"])
        if voucher_id in seen_voucher_ids:
            raise RuntimeError(
                "report source returned a duplicate voucher identity"
            )
        seen_voucher_ids.add(voucher_id)
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
        expired = _expired_at_report_time(
            persisted_expired=bool(raw["expired"]),
            expires_at=raw["expires_at"],
            generated_at=generated_at,
        )
        nominal_raw = raw["is_nominal"]
        is_nominal = None if nominal_raw is None else bool(nominal_raw)
        assigned_to = str(raw["assigned_to"] or "").strip()
        recipient = assigned_to or str(raw["name"] or "").strip()
        ever_used = bool(raw["ever_used"])
        usage_observed = bool(raw["usage_observed"])
        nominality_redacted = bool(raw["nominality_redacted"])
        if ever_used and not usage_observed:
            raise RuntimeError(
                "report source contains used voucher without usage evidence"
            )
        if nominality_redacted and is_nominal is not None:
            raise RuntimeError(
                "report source contains both nominal classification and redaction"
            )
        row = ReportRow(
            voucher_id=voucher_id,
            controller_name=controller_name,
            code=clear_code,
            recipient=recipient,
            created_at=str(raw["created_at"] or ""),
            imported_at=str(raw["imported_at"] or ""),
            expires_at=str(raw["expires_at"] or ""),
            authorized_guest_count=int(raw["authorized_guest_count"] or 0),
            ever_used=ever_used,
            usage_observed=usage_observed,
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
                security_revoked=bool(raw["security_revoked_at"]),
            ),
            origin=str(raw["origin"] or "UNKNOWN"),
            is_nominal=is_nominal,
            last_synced_at=str(raw["last_synced_at"] or ""),
            nominality_redacted=nominality_redacted,
            security_revoked_at=str(raw["security_revoked_at"] or ""),
        )
        if _matches(kind, row):
            rows.append(row)

    if controller_id is None:
        controller_label = "Tutto lo storico locale"
    else:
        controller_label = (
            database.controller_name(controller_id)
            or (next(iter(controllers)) if len(controllers) == 1 else "Controller selezionato")
        )

    materialized = tuple(rows)
    sync_times = sorted(
        row.last_synced_at for row in materialized if row.last_synced_at
    )
    return ReportDataset(
        kind=kind,
        purpose=purpose,
        title=REPORT_TITLES[kind],
        generated_at=str(generated_at),
        controller_label=controller_label,
        rows=materialized,
        totals=_totals(materialized),
        code_exposed=code_exposed,
        data_from=sync_times[0] if sync_times else "",
        data_as_of=sync_times[-1] if sync_times else "",
    )


def build_report_dataset_from_path(
    database_path: Path,
    *,
    kind: ReportKind,
    generated_at: str,
    controller_id: int | None = None,
    include_code_requested: bool = False,
) -> ReportDataset:
    """Build one report through a worker-owned SQLite connection."""

    database = Database(Path(database_path))
    try:
        database.initialize()
        return build_report_dataset(
            database,
            kind=kind,
            generated_at=generated_at,
            controller_id=controller_id,
            include_code_requested=include_code_requested,
        )
    finally:
        database.close()
