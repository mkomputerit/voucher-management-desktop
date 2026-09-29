"""Privacy-safe reporting models built from durable SQLite facts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable

from .database import Database
from .report_policy import ReportPurpose, report_code_value, voucher_code_policy


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
    FULL_HISTORY = "full_history"


REPORT_TITLES = {
    ReportKind.SUMMARY: "Riepilogo storico voucher",
    ReportKind.GENERATED: "Creati con questo software",
    ReportKind.GENERATED_UNUSED: "Creati con questo software - nessun utilizzo rilevato",
    ReportKind.USED: "Voucher utilizzati",
    ReportKind.EXPIRED: "Voucher scaduti",
    ReportKind.PRINTED: "Voucher stampati",
    ReportKind.PRINTED_UNUSED: "Stampati - nessun utilizzo rilevato",
    ReportKind.NEVER_PRINTED: "Voucher senza stampe registrate",
    ReportKind.NOMINAL: "Voucher nominali",
    ReportKind.NON_NOMINAL: "Voucher non nominali",
    ReportKind.UNCLASSIFIED: "Voucher non classificati",
    ReportKind.USAGE_UNKNOWN: "Voucher con utilizzo non determinabile",
    ReportKind.ORIGIN_UNKNOWN: "Voucher con origine creazione non determinabile",
    ReportKind.NOMINALITY_REDACTED: "Nominalità rimossa per privacy",
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
    controller_description: str = ""


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
    coverage_note: str = ""


def _purpose_for_kind(kind: ReportKind) -> ReportPurpose:
    return ReportPurpose.AUDIT if kind is ReportKind.FULL_HISTORY else ReportPurpose.SUMMARY


def _operator_label(value: str) -> str:
    """Translate internal audit sentinels without inventing an operator."""

    normalized = str(value or "").strip()
    if normalized.upper() == "MIGRATION":
        return "Importazione storica"
    if normalized.casefold() == "unknown":
        return "Operatore non determinato"
    return normalized


def _operators(value: object) -> tuple[str, ...]:
    values = {
        _operator_label(item)
        for item in str(value or "").split(",")
        if item.strip()
    }
    return tuple(sorted((item for item in values if item), key=str.casefold))


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _time_bounds(values: Iterable[str]) -> tuple[str, str]:
    """Return chronological ISO bounds without relying on lexicographic offsets."""

    parsed: list[tuple[datetime, str]] = []
    for value in values:
        original = str(value or "").strip()
        moment = _parse_time(original)
        if moment is None:
            continue
        if moment.tzinfo is None:
            normalized = moment.replace(tzinfo=timezone.utc)
        else:
            normalized = moment.astimezone(timezone.utc)
        parsed.append((normalized, original))
    if not parsed:
        return "", ""
    parsed.sort(key=lambda item: item[0])
    return parsed[0][1], parsed[-1][1]


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
    usage_observed: bool,
    print_jobs: int,
) -> str:
    if archived:
        return "Archiviato"
    if expired:
        return "Scaduto"
    if usage_observed and ever_used:
        return "Utilizzato"
    if print_jobs > 0:
        return "Stampato"
    if not usage_observed:
        return "Utilizzo non determinabile"
    return "Senza stampe registrate"


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
        return row.usage_observed and row.ever_used
    if kind is ReportKind.EXPIRED:
        return row.expired
    if kind is ReportKind.PRINTED:
        return row.print_jobs > 0
    if kind is ReportKind.PRINTED_UNUSED:
        return row.print_jobs > 0 and row.usage_observed and not row.ever_used
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
    raise ValueError(f"Unsupported report kind: {kind}")


def _totals(
    rows: Iterable[ReportRow],
    *,
    distinct_print_jobs: int | None = None,
    distinct_reprint_jobs: int | None = None,
) -> ReportTotals:
    materialized = tuple(rows)
    return ReportTotals(
        vouchers=len(materialized),
        generated_vouchers=sum(row.origin in APPLICATION_ORIGINS for row in materialized),
        used_vouchers=sum(
            row.usage_observed and row.ever_used for row in materialized
        ),
        never_used_vouchers=sum(
            row.usage_observed and not row.ever_used for row in materialized
        ),
        usage_unknown_vouchers=sum(
            not row.usage_observed for row in materialized
        ),
        total_controller_uses=sum(
            row.authorized_guest_count
            for row in materialized
            if row.usage_observed
        ),
        expired_vouchers=sum(row.expired for row in materialized),
        printed_vouchers=sum(row.print_jobs > 0 for row in materialized),
        print_jobs=(
            sum(row.print_jobs for row in materialized)
            if distinct_print_jobs is None
            else int(distinct_print_jobs)
        ),
        physical_copies=sum(row.physical_copies for row in materialized),
        reprint_jobs=(
            sum(row.reprint_jobs for row in materialized)
            if distinct_reprint_jobs is None
            else int(distinct_reprint_jobs)
        ),
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
    )


def _build_report_dataset_snapshot(
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
    code_policy = voucher_code_policy(
        purpose,
        include_code_requested=include_code_requested,
    )
    raw_rows = database.report_voucher_rows(
        controller_id=controller_id,
        include_voucher_code=code_policy.expose_code,
    )

    all_rows: list[ReportRow] = []
    rows: list[ReportRow] = []
    controllers: set[str] = set()
    code_exposed = code_policy.expose_code
    for raw in raw_rows:
        controller_name = str(raw["controller_name"] or "").strip() or "Controller"
        controllers.add(controller_name)
        clear_code = report_code_value(
            str(raw["code"] or ""),
            purpose,
            include_code_requested=include_code_requested,
        )
        legacy = bool(raw["legacy_source"])
        print_jobs = int(raw["print_jobs"] or 0)
        expired = _expired_at_report_time(
            persisted_expired=bool(raw["expired"]) and not legacy,
            expires_at=None if legacy else raw["expires_at"],
            generated_at=generated_at,
        )
        nominal_raw = raw["is_nominal"]
        is_nominal = None if nominal_raw is None else bool(nominal_raw)
        assigned_to = str(raw["assigned_to"] or "").strip()
        controller_description = str(raw["name"] or "").strip()
        ever_used = bool(raw["ever_used"])
        # usage_observed is the coverage/provenance gate. A contradictory
        # migrated row may carry ever_used=1 while its usage provenance is
        # explicitly unavailable; reporting must remain conservative and keep
        # that row in "usage unknown" rather than promoting it to confirmed use.
        usage_observed = bool(raw["usage_observed"])
        row = ReportRow(
            voucher_id=int(raw["voucher_id"]),
            controller_name=controller_name,
            code=clear_code,
            recipient=assigned_to,
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
                usage_observed=usage_observed,
                print_jobs=print_jobs,
            ),
            origin=str(raw["origin"] or "UNKNOWN"),
            is_nominal=is_nominal,
            last_synced_at="" if legacy else str(raw["last_synced_at"] or ""),
            nominality_redacted=bool(raw["nominality_redacted"]),
            controller_description=controller_description,
        )
        if legacy:
            row = replace(row, status="Backup precedente - scadenza non verificata", expires_at="")
        all_rows.append(row)
        if _matches(kind, row):
            rows.append(row)

    if kind is not ReportKind.SUMMARY and rows:
        details = database.report_voucher_personal_details(
            voucher_ids=[row.voucher_id for row in rows],
        )
        if len(details) != len(rows):
            raise RuntimeError(
                "Report detail rows changed while the report was being built"
            )
        rows = [
            replace(
                row,
                controller_description=str(details[row.voucher_id]["name"] or "").strip(),
                recipient=str(details[row.voucher_id]["assigned_to"] or "").strip(),
                print_operators=_operators(details[row.voucher_id]["print_operators"]),
            )
            for row in rows
        ]

    if controller_id is None:
        controller_label = "Tutto lo storico locale"
    else:
        controller_label = (
            database.controller_name(controller_id)
            or (next(iter(controllers)) if len(controllers) == 1 else "Controller selezionato")
        )

    # The summary is aggregate-only by design: do not retain per-voucher
    # personal/operator detail in the renderer input when it cannot be shown.
    materialized = () if kind is ReportKind.SUMMARY else tuple(rows)
    totals_source = all_rows if kind is ReportKind.SUMMARY else rows
    data_from, data_as_of = _time_bounds(
        row.last_synced_at for row in all_rows if row.last_synced_at
    )
    unknown_origin = sum(row.origin != "APPLICATION" for row in all_rows)
    unknown_usage = sum(not row.usage_observed for row in all_rows)
    unclassified = sum(
        row.is_nominal is None and not row.nominality_redacted
        for row in all_rows
    )
    redacted = sum(row.nominality_redacted for row in all_rows)
    legacy_count = sum(bool(raw["legacy_source"]) for raw in raw_rows)
    coverage_note = (
        f"Ambito: {len(all_rows)} registrazioni locali. Informazioni non determinabili: "
        f"origine creazione {unknown_origin}, utilizzo {unknown_usage}, "
        f"nominalità non classificata {unclassified}; nominalità rimossa per privacy {redacted}. "
        f"Registrazioni da backup precedente: {legacy_count}; la loro importazione non prova "
        "scadenza né utilizzo e non è una sincronizzazione controller. "
        "Senza stampe registrate significa senza evidenze associate a questa identità locale, "
        "non necessariamente mai stampato. I job di stampa sono invii documento unici; "
        "le copie fisiche contano invece le copie dei singoli voucher associate ai job. "
        "Descrizione UniFi e destinatario locale sono mantenuti separati: uno non prova "
        "il significato dell'altro."
    )
    if not all_rows:
        coverage_note = (
            "Archivio locale senza registrazioni nell'ambito scelto. "
            + coverage_note
        )
    elif kind is not ReportKind.SUMMARY and not rows:
        coverage_note = (
            "Nessun risultato per i criteri scelti. Le informazioni non determinabili "
            "possono escludere voucher dal report. "
            + coverage_note
        )
    print_job_totals = database.report_print_job_totals(
        voucher_ids=[row.voucher_id for row in totals_source],
    )
    return ReportDataset(
        kind=kind,
        purpose=purpose,
        title=REPORT_TITLES[kind],
        generated_at=str(generated_at),
        controller_label=controller_label,
        rows=materialized,
        totals=_totals(
            totals_source,
            distinct_print_jobs=print_job_totals.print_jobs,
            distinct_reprint_jobs=print_job_totals.reprint_jobs,
        ),
        code_exposed=code_exposed,
        data_from=data_from,
        data_as_of=data_as_of,
        coverage_note=coverage_note,
    )


def build_report_dataset(
    database: Database,
    *,
    kind: ReportKind,
    generated_at: str,
    controller_id: int | None = None,
    include_code_requested: bool = False,
) -> ReportDataset:
    """Build one report from a single stable SQLite read snapshot."""

    with database.read_snapshot():
        return _build_report_dataset_snapshot(
            database,
            kind=kind,
            generated_at=generated_at,
            controller_id=controller_id,
            include_code_requested=include_code_requested,
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
