"""Controller-independent voucher lifecycle policy."""

from __future__ import annotations

from dataclasses import dataclass

from .history import PrintStats
from .unifi_api import ApiVoucher


@dataclass(frozen=True)
class DeletePolicyResult:
    """Explain whether the operator tool may delete a voucher."""

    allowed: bool
    reason: str = ""


def evaluate_delete_policy(
    voucher: ApiVoucher,
    stats: PrintStats | None,
) -> DeletePolicyResult:
    """Allow generic cleanup only before issue/use of the voucher.

    Issued, printed or used vouchers are deliberately excluded from this
    generic delete action. Printed-unused credentials may instead enter the
    separate reviewed security-revocation workflow when its evidence and
    operator-configured policy requirements are satisfied.
    """
    if voucher.status == "EXPIRED":
        return DeletePolicyResult(False, "expired")

    if voucher.used > 0 or voucher.status == "USED_MULTIPLE":
        return DeletePolicyResult(False, "in_use")

    if stats and stats.print_jobs > 0:
        return DeletePolicyResult(False, "printed")

    if stats and (
        stats.generated_documents > 0
        or stats.generated_copies > 0
    ):
        return DeletePolicyResult(False, "generated")

    return DeletePolicyResult(True)
