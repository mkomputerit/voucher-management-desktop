"""Read-only live contract smoke test for the official UniFi Network API.

This engineering tool performs no voucher mutation. The API key is collected
with getpass and stays in process memory only. Output is deliberately
privacy-safe: voucher codes, names, UUIDs and the raw Site UUID are never
printed.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
from collections import Counter

from voucher_management.unifi_api import UniFiApiError, UniFiClient


def _fingerprint(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def build_contract_summary(info: dict, vouchers) -> dict[str, object]:
    """Return non-sensitive facts suitable for a field-test transcript."""

    usage_shapes = Counter()
    states = Counter()
    for voucher in vouchers:
        if voucher.quota == 0:
            usage_shapes["unlimited"] += 1
        elif voucher.quota == 1:
            usage_shapes["single"] += 1
        else:
            usage_shapes["multi"] += 1

        if voucher.status == "EXPIRED":
            states["expired"] += 1
        elif voucher.used > 0:
            states["used"] += 1
        else:
            states["unused"] += 1

    return {
        "network_version": str(info.get("applicationVersion") or ""),
        "site_fingerprint": _fingerprint(str(info.get("siteId") or "")),
        "voucher_count": len(vouchers),
        "usage_shapes": dict(sorted(usage_shapes.items())),
        "states": dict(sorted(states.items())),
    }


def run_live_contract(
    *,
    api_root: str,
    api_key: str,
    preferred_site_id: str | None = None,
    trusted_cert_sha256: str | None = None,
) -> dict[str, object]:
    """Connect read-only, list vouchers and verify one direct UUID read."""

    client = UniFiClient(
        api_root,
        trusted_cert_sha256=trusted_cert_sha256,
        preferred_site_id=preferred_site_id,
    )
    info = client.connect(api_key)
    vouchers = client.list_vouchers()

    if vouchers:
        probe = vouchers[0]
        detail = client.get_voucher(probe.id)
        if detail.id != probe.id:
            raise UniFiApiError(
                "Il dettaglio UUID non corrisponde alla voce dell'elenco"
            )
        if detail.code != probe.code:
            raise UniFiApiError(
                "Il dettaglio voucher non corrisponde al codice dell'elenco"
            )

    return build_contract_summary(info, vouchers)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verifica read-only del contratto API UniFi usato da Voucher Management."
    )
    parser.add_argument("api_root", help="Root Integration API, senza credenziali")
    parser.add_argument(
        "--site-id",
        default="",
        help="Site UUID già verificato, utile su controller multi-site",
    )
    parser.add_argument(
        "--cert-sha256",
        default="",
        help="Pin SHA-256 già verificato per controller con certificato locale",
    )
    args = parser.parse_args(argv)

    api_key = getpass.getpass("API key UniFi (solo memoria): ")
    try:
        summary = run_live_contract(
            api_root=args.api_root,
            api_key=api_key,
            preferred_site_id=args.site_id.strip() or None,
            trusted_cert_sha256=args.cert_sha256.strip() or None,
        )
    except (UniFiApiError, ValueError) as exc:
        print(f"CONTRACT FAIL: {exc}")
        return 1
    finally:
        api_key = ""

    print("CONTRACT OK")
    print(f"Network: {summary['network_version']}")
    print(f"Site fingerprint: {summary['site_fingerprint']}")
    print(f"Voucher osservati: {summary['voucher_count']}")
    print(f"Tipi: {summary['usage_shapes']}")
    print(f"Stati: {summary['states']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
