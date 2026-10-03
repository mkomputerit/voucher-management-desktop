"""Tk-independent voucher application workflows.

This module owns controller/cache orchestration and print-batch construction so
the Tk layer can remain presentation-only. Network and history implementations
are still injected by callers, which keeps these workflows directly testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .database import PRINT_STATE_NOT_PRINTED, PRINT_STATE_PRINTED, PRINT_STATE_UNKNOWN
from .history import PrintStats
from .models import VoucherBatch, VoucherRecord
from .policy import DeletePolicyResult, evaluate_delete_policy
from .print_archive import build_voucher_pdf_path
from .unifi_api import (
    ApiVoucher,
    UniFiApiError,
    UniFiClient,
    UniFiMutationUncertain,
    UniFiVoucherNotFound,
)
from .utils import find_file_by_exact_name


@dataclass(frozen=True)
class CreateOutcome:
    """Result of a controller create attempt and its reconciliation."""

    created: tuple[ApiVoucher, ...]
    vouchers: tuple[ApiVoucher, ...]
    refresh_error: UniFiApiError | None = None
    uncertain_error: UniFiMutationUncertain | None = None
    local_persistence_error: Exception | None = None
    recovery_marker_error: Exception | None = None
    snapshot_complete: bool = True
    reconciliation_required: bool = False


@dataclass(frozen=True)
class DeleteBlock:
    """One voucher rejected by lifecycle policy."""

    voucher: ApiVoucher
    policy: DeletePolicyResult


@dataclass(frozen=True)
class DeleteOutcome:
    """Result of a confirmed controller delete and its local reconciliation."""

    vouchers: tuple[ApiVoucher, ...]
    refresh_error: UniFiApiError | None = None
    local_persistence_error: Exception | None = None
    reconciliation_required: bool = False



@dataclass(frozen=True)
class PrintJob:
    """Fully resolved print-generation request, independent of Tk."""

    batch: VoucherBatch
    output: Path


@dataclass(frozen=True)
class PrintJobOutcome:
    """Result of rendering and auditing one print-generation request."""

    output: Path
    codes: tuple[str, ...]
    reprint: bool


@dataclass(frozen=True)
class ExistingPdfResolution:
    """Verified archived PDF and every known voucher linked to it."""

    path: Path
    linked_codes: tuple[str, ...]
    linked_voucher_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SnapshotAbsenceOutcome:
    """Result of direct checks for repeatedly omitted voucher UUIDs."""

    vouchers: tuple[ApiVoucher, ...]
    confirmed_absent_ids: frozenset[str]
    unresolved_ids: frozenset[str]
    recovered_ids: frozenset[str]


class ExistingPdfResolutionError(RuntimeError):
    """Typed failure while resolving a voucher to an archived PDF."""

    def __init__(
        self,
        reason: str,
        *,
        path: Path | None = None,
    ):
        super().__init__(reason)
        self.reason = reason
        self.path = path


def _required_int(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not an integer input")
    return int(value)


def _optional_positive_int(
    value: object,
    *,
    maximum: int | None = None,
) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not an integer input")
    text = str(value).strip()
    if not text:
        return None
    number = int(text)
    if number <= 0:
        raise ValueError("value must be positive")
    if maximum is not None and number > maximum:
        raise ValueError("value exceeds supported maximum")
    return number


def validate_create_params(
    *,
    recipient: object,
    quantity: object,
    mode: object,
    quota: object,
    expire_number: object,
    expire_unit: object,
    data_mb: object = None,
    down_mbps: object = None,
    up_mbps: object = None,
    is_nominal: object = False,
) -> dict[str, object]:
    """Normalize and validate CreateDialog values without any Tk dependency."""

    name = str(recipient).strip()
    if not name:
        raise ValueError("recipient is required")

    qty = _required_int(quantity)
    if not 1 <= qty <= 50:
        raise ValueError("quantity out of range")

    expiration = _required_int(expire_number)
    if expiration < 1:
        raise ValueError("expiration must be positive")

    normalized_mode = str(mode)
    if normalized_mode == "Monouso":
        normalized_quota = 1
    elif normalized_mode == "Multiuso illimitato":
        normalized_quota = 0
    elif normalized_mode == "Multiuso":
        normalized_quota = _required_int(quota)
        if not 2 <= normalized_quota <= 999:
            raise ValueError("quota out of range")
    else:
        raise ValueError("unsupported usage mode")

    units = {
        "Minuti": 1,
        "Ore": 60,
        "Giorni": 1440,
    }
    try:
        expiration_unit = units[str(expire_unit)]
    except KeyError as exc:
        raise ValueError("unsupported expiration unit") from exc

    data_limit = _optional_positive_int(
        data_mb,
        maximum=1_048_576,
    )
    download_limit = _optional_positive_int(
        down_mbps,
        maximum=100,
    )
    upload_limit = _optional_positive_int(
        up_mbps,
        maximum=100,
    )

    return {
        "recipient": name,
        "quantity": qty,
        "expire_number": expiration,
        "expire_unit": expiration_unit,
        "quota": normalized_quota,
        "data_mb": data_limit,
        "down_mbps": download_limit,
        "up_mbps": upload_limit,
        "is_nominal": bool(is_nominal),
    }


def verify_print_history_ready(
    selected: Sequence[ApiVoucher],
    *,
    history,
    settings: Mapping[str, object],
) -> None:
    """Fail before PDF preparation when the local audit history is unusable."""

    history.stats_for_codes(
        [voucher.code_formatted for voucher in selected],
        settings,
    )


def prepare_print_job(
    selected: Sequence[ApiVoucher],
    prints_root: Path,
    *,
    unlimited_copies: int = 1,
    now: datetime,
    site_id: str = "",
) -> PrintJob:
    """Resolve selected vouchers to one deterministic PDF-generation job."""

    batch = build_print_batch(
        selected,
        unlimited_copies=unlimited_copies,
        site_id=site_id,
    )
    output = build_voucher_pdf_path(
        Path(prints_root),
        batch.recipient or "Voucher",
        now,
    )
    return PrintJob(batch=batch, output=output)


def execute_print_job(
    job: PrintJob,
    *,
    history,
    settings: Mapping[str, object],
    render_pdf: Callable[[VoucherBatch, Path, Mapping[str, object]], object],
) -> PrintJobOutcome:
    """Render and audit one print job without any Tk dependency."""

    render_pdf(job.batch, job.output, settings)
    if hasattr(history, "find_duplicates_for_batch"):
        duplicates = history.find_duplicates_for_batch(
            job.batch,
            settings,
        )
    else:
        duplicates = history.find_duplicates(
            job.batch.codes,
            settings,
        )
    reprint = bool(duplicates)
    history.record_batch(
        job.batch,
        job.output,
        settings,
        reprint=reprint,
    )
    return PrintJobOutcome(
        output=job.output,
        codes=tuple(job.batch.codes),
        reprint=reprint,
    )


def resolve_existing_pdf(
    voucher: ApiVoucher,
    all_vouchers: Sequence[ApiVoucher],
    *,
    history,
    settings: Mapping[str, object],
    prints_root: Path,
    site_id: str = "",
    finder: Callable[[Path, str], Sequence[Path]] = find_file_by_exact_name,
) -> ExistingPdfResolution:
    """Resolve and verify the archived PDF linked to one selected voucher."""

    stat = history.stats_for_codes(
        [voucher.code_formatted],
        settings,
    ).get(voucher.code_formatted)
    if not stat or not stat.latest_output_file:
        raise ExistingPdfResolutionError("not_recorded")

    recorded = Path(stat.latest_output_file)
    path = recorded
    if not recorded.is_absolute() or not recorded.exists():
        matches = sorted(
            finder(Path(prints_root), recorded.name),
            key=lambda item: item.stat().st_mtime,
        )
        if matches:
            path = matches[-1]
        elif not recorded.is_absolute():
            path = Path(prints_root) / recorded.name

    if not path.exists():
        raise ExistingPdfResolutionError(
            "missing_file",
            path=path,
        )

    if hasattr(history, "voucher_links_for_output"):
        links = tuple(
            history.voucher_links_for_output(
                [
                    (str(item.id), item.code_formatted)
                    for item in all_vouchers
                ],
                site_id,
                path,
                settings,
            )
        )
        linked_voucher_ids = tuple(item[0] for item in links)
        linked_codes = tuple(item[1] for item in links)
        if str(voucher.id) not in linked_voucher_ids:
            raise ExistingPdfResolutionError("linkage_mismatch")
    else:
        linked_codes = tuple(
            history.codes_for_output(
                [item.code_formatted for item in all_vouchers],
                path,
                settings,
            )
        )
        linked_voucher_ids = tuple(
            str(item.id)
            for item in all_vouchers
            if item.code_formatted in linked_codes
        )
        if voucher.code_formatted not in linked_codes:
            raise ExistingPdfResolutionError("linkage_mismatch")

    return ExistingPdfResolution(
        path=path,
        linked_codes=linked_codes,
        linked_voucher_ids=linked_voucher_ids,
    )


def refresh_vouchers(client: UniFiClient) -> list[ApiVoucher]:
    """Fetch the current controller voucher list."""

    return client.list_vouchers()


def verify_snapshot_absences(
    client: UniFiClient,
    snapshot: Sequence[ApiVoucher],
    candidate_ids: Sequence[str],
) -> SnapshotAbsenceOutcome:
    """Directly verify UUIDs omitted by repeated complete list snapshots.

    Positive GET results are merged back into the working snapshot. A typed
    voucher 404 is the only evidence accepted as confirmed absence. Transport
    failures remain unresolved and are never converted into deletion facts.
    """

    by_id = {
        str(voucher.id): voucher
        for voucher in snapshot
    }
    confirmed: set[str] = set()
    unresolved: set[str] = set()
    recovered: set[str] = set()

    for value in candidate_ids:
        remote_id = str(value or "").strip()
        if not remote_id or remote_id in by_id:
            continue
        try:
            live = client.get_voucher(remote_id)
        except UniFiVoucherNotFound:
            confirmed.add(remote_id)
        except UniFiApiError:
            unresolved.add(remote_id)
        else:
            if str(live.id).strip() != remote_id:
                unresolved.add(remote_id)
                continue
            by_id[remote_id] = live
            recovered.add(remote_id)

    return SnapshotAbsenceOutcome(
        vouchers=tuple(by_id.values()),
        confirmed_absent_ids=frozenset(confirmed),
        unresolved_ids=frozenset(unresolved),
        recovered_ids=frozenset(recovered),
    )


def create_vouchers_and_refresh(
    client: UniFiClient,
    cached_vouchers: Sequence[ApiVoucher],
    params: Mapping[str, object],
) -> CreateOutcome:
    """Create once, then refresh without hiding a successful mutation.

    If the controller accepts creation but the subsequent list refresh fails,
    merge the returned vouchers into the local cache. The caller can therefore
    report "created, refresh failed" rather than encouraging a duplicate create.
    """

    controller_params = dict(params)
    # Nominality is an application-only reporting classification. UniFi has no
    # corresponding field and must receive exactly the same voucher payload as
    # before this feature existed.
    controller_params.pop("is_nominal", None)

    try:
        created = tuple(client.create_vouchers(**controller_params))
    except UniFiMutationUncertain as exc:
        # Never retry a non-idempotent POST automatically. A fresh GET is safe
        # and gives the operator the best available controller state, but in a
        # multi-station setup new rows cannot be attributed to this instance
        # with certainty.
        try:
            refreshed = tuple(client.list_vouchers())
            return CreateOutcome(
                created=(),
                vouchers=refreshed,
                uncertain_error=exc,
            )
        except UniFiApiError as refresh_exc:
            return CreateOutcome(
                created=(),
                vouchers=tuple(cached_vouchers),
                refresh_error=refresh_exc,
                uncertain_error=exc,
            )

    try:
        refreshed = tuple(client.list_vouchers())
        refreshed_by_id = {voucher.id: voucher for voucher in refreshed}
        missing_created = [
            voucher for voucher in created if voucher.id not in refreshed_by_id
        ]
        if missing_created:
            # A successful HTTP response is not enough to assume read-after-write
            # consistency. Keep every confirmed POST result visible and avoid
            # treating this list as a complete absence-authoritative snapshot.
            merged = dict(refreshed_by_id)
            for voucher in missing_created:
                merged[voucher.id] = voucher
            return CreateOutcome(
                created=created,
                vouchers=tuple(merged.values()),
                snapshot_complete=False,
                reconciliation_required=True,
            )
        return CreateOutcome(
            created=created,
            vouchers=refreshed,
            snapshot_complete=True,
        )
    except UniFiApiError as exc:
        merged = {voucher.id: voucher for voucher in cached_vouchers}
        for voucher in created:
            merged[voucher.id] = voucher
        return CreateOutcome(
            created=created,
            vouchers=tuple(merged.values()),
            refresh_error=exc,
            snapshot_complete=False,
            reconciliation_required=True,
        )


def refresh_delete_candidates(
    client: UniFiClient,
    selected: Sequence[ApiVoucher],
) -> list[ApiVoucher]:
    """Re-read every selected voucher immediately before delete policy."""

    return [client.get_voucher(voucher.id) for voucher in selected]


def evaluate_delete_candidates(
    vouchers: Sequence[ApiVoucher],
    stats_by_code: Mapping[str, PrintStats],
    *,
    historically_used_ids: frozenset[str],
    local_print_states: Mapping[str, str],
    aligned_ids: frozenset[str],
    exceptionally_deletable_ids: frozenset[str] = frozenset(),
) -> list[DeleteBlock]:
    """Return every voucher blocked by controller or durable local lifecycle facts.

    Ordinary deletion is a preparation-error correction.  It is available only
    when the local database positively proves that the voucher is aligned and
    has print_state=NOT_PRINTED.  Missing/unknown local evidence fails closed.
    """

    blocked: list[DeleteBlock] = []
    for voucher in vouchers:
        remote_id = str(voucher.id)
        if remote_id in historically_used_ids:
            result = DeletePolicyResult(False, "in_use")
        elif remote_id in exceptionally_deletable_ids:
            result = evaluate_delete_policy(
                voucher,
                stats_by_code.get(voucher.code_formatted),
            )
        elif remote_id not in aligned_ids:
            result = DeletePolicyResult(False, "not_aligned")
        else:
            print_state = str(
                local_print_states.get(remote_id, PRINT_STATE_UNKNOWN)
                or PRINT_STATE_UNKNOWN
            )
            if print_state == PRINT_STATE_PRINTED:
                result = DeletePolicyResult(False, "printed")
            elif print_state != PRINT_STATE_NOT_PRINTED:
                result = DeletePolicyResult(False, "print_unknown")
            else:
                result = evaluate_delete_policy(
                    voucher,
                    stats_by_code.get(voucher.code_formatted),
                )
        if not result.allowed:
            blocked.append(DeleteBlock(voucher=voucher, policy=result))
    return blocked


def delete_vouchers_and_refresh(
    client: UniFiClient,
    cached_vouchers: Sequence[ApiVoucher],
    current: Sequence[ApiVoucher],
) -> DeleteOutcome:
    """Delete verified vouchers and refresh, with a safe local fallback.

    A delete error itself is intentionally not swallowed: callers need to
    distinguish an uncertain/partial mutation from a completed mutation whose
    follow-up refresh failed.
    """

    ids = [voucher.id for voucher in current]
    client.delete_vouchers(ids)

    try:
        refreshed = tuple(client.list_vouchers())
        deleted_ids = set(ids)
        stale_deleted_present = any(
            voucher.id in deleted_ids
            for voucher in refreshed
        )
        visible = tuple(
            voucher
            for voucher in refreshed
            if voucher.id not in deleted_ids
        )
        return DeleteOutcome(
            vouchers=visible,
            reconciliation_required=stale_deleted_present,
        )
    except UniFiApiError as exc:
        deleted_ids = set(ids)
        fallback = tuple(
            voucher
            for voucher in cached_vouchers
            if voucher.id not in deleted_ids
        )
        return DeleteOutcome(
            vouchers=fallback,
            refresh_error=exc,
        )


def build_print_batch(
    selected: Sequence[ApiVoucher],
    *,
    unlimited_copies: int = 1,
    site_id: str = "",
) -> VoucherBatch:
    """Build controller-independent print records from selected vouchers."""

    if not selected:
        raise ValueError("Nessun voucher selezionato")
    if not 1 <= int(unlimited_copies) <= 999:
        raise ValueError("Numero copie non valido")

    voucher_ids = [str(voucher.id).strip() for voucher in selected]
    if any(not voucher_id for voucher_id in voucher_ids):
        raise ValueError("Identità voucher mancante nella selezione")
    if len(set(voucher_ids)) != len(voucher_ids):
        raise ValueError("Selezione voucher duplicata")

    canonical_codes = [
        str(voucher.code_formatted).strip().replace("-", "")
        for voucher in selected
    ]
    if any(not code for code in canonical_codes):
        raise ValueError("Codice voucher mancante nella selezione")
    if len(set(canonical_codes)) != len(canonical_codes):
        raise ValueError(
            "Codice voucher duplicato o ambiguo nella selezione"
        )

    records: list[VoucherRecord] = []
    only_unlimited = len(selected) == 1 and selected[0].quota == 0

    for voucher in selected:
        repeat = int(unlimited_copies) if only_unlimited else 1
        records.extend(
            VoucherRecord(
                code=voucher.code_formatted,
                duration_minutes=voucher.duration_minutes,
                recipient=voucher.recipient or "Guest",
                unifi_id=str(voucher.id),
            )
            for _ in range(repeat)
        )

    return VoucherBatch(
        source_path=Path("CONTROLLER_API"),
        vouchers=records,
        recipient=records[0].recipient if records else "",
        site_id=str(site_id or "").strip(),
    )
