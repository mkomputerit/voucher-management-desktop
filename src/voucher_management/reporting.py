"""Privacy-safe reporting models built from durable SQLite facts."""

from __future__ import annotations

from dataclasses import dataclass
import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterable

from .database import Database
from .operational_alerts import (
    unprinted_warning_candidates,
    unprinted_warning_days,
)
from .report_policy import ReportPurpose, report_code_value
from .security_revocation import (
    security_revocation_candidates,
    security_revoke_days,
)


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
    PRINT_UNKNOWN = "print_unknown"
    UNPRINTED_WARNING = "unprinted_warning"
    SECURITY_REVIEW = "security_review"
    NOMINAL = "nominal"
    NON_NOMINAL = "non_nominal"
    UNCLASSIFIED = "unclassified"
    USAGE_UNKNOWN = "usage_unknown"
    ORIGIN_UNKNOWN = "origin_unknown"
    NOMINALITY_REDACTED = "nominality_redacted"
    SECURITY_REVOKED = "security_revoked"
    PREPARATION_DELETED = "preparation_deleted"
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
    ReportKind.PRINT_UNKNOWN: "Voucher con stato stampa non determinabile",
    ReportKind.UNPRINTED_WARNING: "Voucher creati ma non stampati oltre soglia",
    ReportKind.SECURITY_REVIEW: "Voucher da revocare per sicurezza",
    ReportKind.NOMINAL: "Voucher nominali",
    ReportKind.NON_NOMINAL: "Voucher non nominali",
    ReportKind.UNCLASSIFIED: "Voucher non classificati",
    ReportKind.USAGE_UNKNOWN: "Voucher con utilizzo non determinabile",
    ReportKind.ORIGIN_UNKNOWN: "Voucher con origine creazione non determinabile",
    ReportKind.NOMINALITY_REDACTED: "Nominalità rimossa per privacy",
    ReportKind.SECURITY_REVOKED: "Voucher revocati per sicurezza",
    ReportKind.PREPARATION_DELETED: "Voucher eliminati dalla controller",
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
    print_unknown_vouchers: int = 0
    preparation_deleted_vouchers: int = 0


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
    activated_at: str
    duration_minutes: int
    authorized_guest_limit: int | None
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
    last_seen_at: str = ""
    last_synced_at: str = ""
    nominality_redacted: bool = False
    security_revoked_at: str = ""
    unifi_id: str = ""
    unifi_name: str = ""
    local_notes: str = ""
    print_state: str = "UNKNOWN"
    preparation_deleted_at: str = ""
    preparation_delete_reason: str = ""
    controller_deletion_source: str = ""


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
    print_state: str,
    print_jobs: int,
    security_revoked: bool = False,
    preparation_deleted: bool = False,
) -> str:
    if security_revoked:
        return "Revocato per sicurezza"
    if preparation_deleted:
        return "Eliminato dalla controller"
    if archived:
        return "Archiviato"
    if expired:
        return "Scaduto"
    if ever_used:
        return "Utilizzato"
    normalized_print_state = str(print_state or "UNKNOWN").strip().upper()
    if print_jobs > 0 or normalized_print_state == "PRINTED":
        return "Stampato"
    if normalized_print_state == "NOT_PRINTED":
        return "Mai stampato"
    return "Stampa non determinabile"


def _is_printed(row: ReportRow) -> bool:
    return row.print_jobs > 0 or row.print_state == "PRINTED"


def _is_not_printed(row: ReportRow) -> bool:
    return row.print_jobs == 0 and row.print_state == "NOT_PRINTED"


def _is_print_unknown(row: ReportRow) -> bool:
    return not _is_printed(row) and not _is_not_printed(row)


def print_state_label(row: ReportRow) -> str:
    if _is_printed(row):
        return "Stampato"
    if _is_not_printed(row):
        return "Non stampato"
    return "Non determinabile"


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
        return _is_printed(row)
    if kind is ReportKind.PRINTED_UNUSED:
        return _is_printed(row) and not row.ever_used
    if kind is ReportKind.NEVER_PRINTED:
        return _is_not_printed(row)
    if kind is ReportKind.PRINT_UNKNOWN:
        return _is_print_unknown(row)
    if kind in {ReportKind.UNPRINTED_WARNING, ReportKind.SECURITY_REVIEW}:
        raise ValueError("threshold report kind requires candidate selection")
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
    if kind is ReportKind.PREPARATION_DELETED:
        return bool(row.preparation_deleted_at)
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
        printed_vouchers=sum(_is_printed(row) for row in materialized),
        print_jobs=sum(row.print_jobs for row in materialized),
        physical_copies=sum(row.physical_copies for row in materialized),
        reprint_jobs=sum(row.reprint_jobs for row in materialized),
        reprint_copies=sum(row.reprint_copies for row in materialized),
        printed_never_used=sum(
            _is_printed(row) and row.usage_observed and not row.ever_used
            for row in materialized
        ),
        never_printed=sum(_is_not_printed(row) for row in materialized),
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
            _is_printed(row) and not row.usage_observed
            for row in materialized
        ),
        print_unknown_vouchers=sum(
            _is_print_unknown(row) for row in materialized
        ),
        preparation_deleted_vouchers=sum(
            bool(row.preparation_deleted_at) for row in materialized
        ),
    )


def _validated_totals(rows: Iterable[ReportRow]) -> ReportTotals:
    """Calculate totals and reject overlapping/incomplete classifications."""

    totals = _totals(rows)
    if (
        totals.used_vouchers
        + totals.never_used_vouchers
        + totals.usage_unknown_vouchers
        != totals.vouchers
    ):
        raise RuntimeError("report usage totals are internally inconsistent")
    if (
        totals.printed_vouchers
        + totals.never_printed
        + totals.print_unknown_vouchers
        != totals.vouchers
    ):
        raise RuntimeError("report print totals are internally inconsistent")
    if (
        totals.nominal_vouchers
        + totals.non_nominal_vouchers
        + totals.unclassified_vouchers
        + totals.redacted_nominality_vouchers
        != totals.vouchers
    ):
        raise RuntimeError("report nominality totals are internally inconsistent")
    if totals.generated_vouchers + totals.unknown_origin_vouchers != totals.vouchers:
        raise RuntimeError("report origin totals are internally inconsistent")
    return totals


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

    threshold_candidate_ids: set[int] | None = None
    report_title = REPORT_TITLES[kind]
    if kind is ReportKind.UNPRINTED_WARNING:
        days = unprinted_warning_days(database)
        threshold_candidate_ids = {
            item.voucher_id
            for item in unprinted_warning_candidates(
                database,
                now=generated_at,
                controller_id=controller_id,
            )
        }
        report_title = (
            f"{report_title} ({days} giorni dalla creazione)"
            if days is not None
            else f"{report_title} (soglia non configurata)"
        )
    elif kind is ReportKind.SECURITY_REVIEW:
        days = security_revoke_days(database)
        threshold_candidate_ids = {
            item.voucher_id
            for item in security_revocation_candidates(
                database,
                now=generated_at,
                controller_id=controller_id,
            )
        }
        report_title = (
            f"{report_title} (soglia {days} giorni; data stampa ignota: revisione immediata)"
            if days is not None
            else f"{report_title} (soglia non configurata)"
        )

    rows: list[ReportRow] = []
    controllers: set[str] = set()
    code_exposed = False
    seen_voucher_ids: set[int] = set()
    seen_remote_ids: set[tuple[int, str]] = set()
    for raw in raw_rows:
        voucher_id = int(raw["voucher_id"])
        if voucher_id in seen_voucher_ids:
            raise RuntimeError(
                "report source returned a duplicate voucher identity"
            )
        seen_voucher_ids.add(voucher_id)
        unifi_id = str(raw["unifi_id"] or "").strip()
        if not unifi_id:
            raise RuntimeError("report source contains voucher without UniFi identity")
        remote_identity = (int(raw["controller_id"]), unifi_id)
        if remote_identity in seen_remote_ids:
            raise RuntimeError(
                "report source returned a duplicate UniFi voucher identity"
            )
        seen_remote_ids.add(remote_identity)
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
        unifi_name = str(raw["name"] or "").strip()
        local_notes = str(raw["notes"] or "").strip()
        ever_used = bool(raw["ever_used"])
        usage_observed = bool(raw["usage_observed"])
        nominality_redacted = bool(raw["nominality_redacted"])
        preparation_deleted_at = str(raw["preparation_deleted_at"] or "")
        delete_event_type = str(
            raw["controller_delete_event_type"] or ""
        ).strip()
        controller_deletion_source = {
            "PREPARATION_DELETED": "Voucher Management",
            "CONTROLLER_DELETED": "Rilevata sulla controller",
        }.get(delete_event_type, "")
        preparation_delete_reason = ""
        raw_delete_details = str(raw["preparation_delete_details"] or "").strip()
        if raw_delete_details and delete_event_type == "PREPARATION_DELETED":
            try:
                parsed_delete_details = json.loads(raw_delete_details)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed_delete_details = {}
            if isinstance(parsed_delete_details, dict):
                preparation_delete_reason = str(
                    parsed_delete_details.get("reason") or ""
                ).strip()
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
            recipient=unifi_name,
            created_at=str(raw["created_at"] or ""),
            imported_at=str(raw["imported_at"] or ""),
            expires_at=str(raw["expires_at"] or ""),
            activated_at=str(raw["activated_at"] or ""),
            duration_minutes=int(raw["duration_minutes"] or 0),
            authorized_guest_limit=(
                None
                if raw["authorized_guest_limit"] is None
                else int(raw["authorized_guest_limit"])
            ),
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
                print_state=str(raw["print_state"] or "UNKNOWN"),
                print_jobs=print_jobs,
                security_revoked=bool(raw["security_revoked_at"]),
                preparation_deleted=bool(preparation_deleted_at),
            ),
            origin=str(raw["origin"] or "UNKNOWN"),
            is_nominal=is_nominal,
            last_seen_at=str(raw["last_seen_at"] or ""),
            last_synced_at=str(raw["last_synced_at"] or ""),
            nominality_redacted=nominality_redacted,
            security_revoked_at=str(raw["security_revoked_at"] or ""),
            unifi_id=unifi_id,
            unifi_name=unifi_name,
            local_notes=local_notes,
            print_state=str(raw["print_state"] or "UNKNOWN").strip().upper(),
            preparation_deleted_at=preparation_deleted_at,
            preparation_delete_reason=preparation_delete_reason,
            controller_deletion_source=controller_deletion_source,
        )
        if threshold_candidate_ids is not None:
            if voucher_id in threshold_candidate_ids:
                rows.append(row)
        elif _matches(kind, row):
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
        row.last_seen_at for row in materialized if row.last_seen_at
    )
    return ReportDataset(
        kind=kind,
        purpose=purpose,
        title=report_title,
        generated_at=str(generated_at),
        controller_label=controller_label,
        rows=materialized,
        totals=_validated_totals(materialized),
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
