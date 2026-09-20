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
    """Allow cleanup only before issue/use of the voucher.

    Voucher Management deliberately does not implement revocation. A voucher
    that has been used, is reported as in-use by the controller, or has a local
    physical print event is outside the cleanup workflow and must be handled by
    the network administrator.
    """
    if voucher.status == "EXPIRED":
        return DeletePolicyResult(False, "expired")

    if voucher.used > 0 or voucher.status == "USED_MULTIPLE":
        return DeletePolicyResult(False, "in_use")

    if stats and stats.print_jobs > 0:
        return DeletePolicyResult(False, "printed")

    return DeletePolicyResult(True)
