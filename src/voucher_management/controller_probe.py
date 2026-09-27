"""Credential-ephemeral UniFi probe used by first-run onboarding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .unifi_api import UniFiClient


@dataclass(frozen=True)
class ControllerProbeResult:
    """Successful controller verification result.

    The connected client intentionally remains an in-memory object because it
    owns the active API key. No credential is copied into this dataclass.
    """

    client: UniFiClient
    info: dict
    vouchers: tuple


def probe_controller(
    api_root: str,
    api_key: str,
    *,
    trusted_cert_sha256: str | None = None,
    client_factory: Callable[..., UniFiClient] = UniFiClient,
) -> ControllerProbeResult:
    """Validate one controller and fetch the initial complete voucher snapshot."""

    client = client_factory(
        api_root,
        trusted_cert_sha256=trusted_cert_sha256,
    )
    info = client.connect(api_key)
    vouchers = tuple(client.list_vouchers())
    return ControllerProbeResult(
        client=client,
        info=dict(info),
        vouchers=vouchers,
    )
