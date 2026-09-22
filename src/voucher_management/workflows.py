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

from .history import PrintStats
from .models import VoucherBatch, VoucherRecord
from .policy import DeletePolicyResult, evaluate_delete_policy
from .print_archive import build_voucher_pdf_path
from .unifi_api import (
    ApiVoucher,
    UniFiApiError,
    UniFiClient,
    UniFiMutationUncertain,
)
from .utils import find_file_by_exact_name


@dataclass(frozen=True)
class CreateOutcome:
    """Result of a controller create attempt and its reconciliation."""

    created: tuple[ApiVoucher, ...]
    vouchers: tuple[ApiVoucher, ...]
    refresh_error: UniFiApiError | None = None
    uncertain_error: UniFiMutationUncertain | None = None


@dataclass(frozen=True)
class DeleteBlock:
    """One voucher rejected by lifecycle policy."""

    voucher: ApiVoucher
    policy: DeletePolicyResult


@dataclass(frozen=True)
class DeleteOutcome:
    """Result of a successful controller delete operation."""

    vouchers: tuple[ApiVoucher, ...]
    refresh_error: UniFiApiError | None = None



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
) -> PrintJob:
    """Resolve selected vouchers to one deterministic PDF-generation job."""

    batch = build_print_batch(
        selected,
        unlimited_copies=unlimited_copies,
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
    reprint = bool(
        history.find_duplicates(
            job.batch.codes,
            settings,
        )
    )
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

    linked_codes = tuple(
        history.codes_for_output(
            [item.code_formatted for item in all_vouchers],
            path,
            settings,
        )
    )
    if voucher.code_formatted not in linked_codes:
        raise ExistingPdfResolutionError("linkage_mismatch")

    return ExistingPdfResolution(
        path=path,
        linked_codes=linked_codes,
    )


def refresh_vouchers(client: UniFiClient) -> list[ApiVoucher]:
    """Fetch the current controller voucher list."""

    return client.list_vouchers()


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

    try:
        created = tuple(client.create_vouchers(**dict(params)))
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
        return CreateOutcome(created=created, vouchers=refreshed)
    except UniFiApiError as exc:
        merged = {voucher.id: voucher for voucher in cached_vouchers}
        for voucher in created:
            merged[voucher.id] = voucher
        return CreateOutcome(
            created=created,
            vouchers=tuple(merged.values()),
            refresh_error=exc,
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
) -> list[DeleteBlock]:
    """Return every voucher blocked by local/controller lifecycle policy."""

    blocked: list[DeleteBlock] = []
    for voucher in vouchers:
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
        return DeleteOutcome(vouchers=refreshed)
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
) -> VoucherBatch:
    """Build controller-independent print records from selected vouchers."""

    if not selected:
        raise ValueError("Nessun voucher selezionato")
    if not 1 <= int(unlimited_copies) <= 999:
        raise ValueError("Numero copie non valido")

    records: list[VoucherRecord] = []
    only_unlimited = len(selected) == 1 and selected[0].quota == 0

    for voucher in selected:
        repeat = int(unlimited_copies) if only_unlimited else 1
        records.extend(
            VoucherRecord(
                code=voucher.code_formatted,
                duration_minutes=voucher.duration_minutes,
                recipient=voucher.recipient or "Guest",
            )
            for _ in range(repeat)
        )

    return VoucherBatch(
        source_path=Path("CONTROLLER_API"),
        vouchers=records,
        recipient=records[0].recipient if records else "",
    )
