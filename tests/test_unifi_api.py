from __future__ import annotations

from datetime import datetime, timezone
import ssl
from urllib.error import HTTPError, URLError

import pytest

from voucher_management.unifi_api import (
    OFFICIAL_LOCAL_API_PATH,
    PinnedCertificateMismatch,
    UniFiApiError,
    UniFiCertificateChanged,
    UniFiClient,
    UniFiMutationUncertain,
    _NoRedirectHandler,
    _PinnedHTTPSConnection,
    normalize_api_root,
)


SITE_ID = "11111111-2222-3333-4444-555555555555"


def voucher_json(
    *,
    voucher_id: str = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    code: str = "1234567890",
    name: str = "TEST",
    guest_limit=1,
    guest_count: int = 0,
    expired: bool = False,
    activated_at=None,
    expires_at=None,
    data_mb=None,
    rx_kbps=None,
    tx_kbps=None,
) -> dict:
    """Build a synthetic official voucher response without production data."""

    item = {
        "id": voucher_id,
        "createdAt": "2026-09-19T16:00:00Z",
        "name": name,
        "code": code,
        "authorizedGuestCount": guest_count,
        "expired": expired,
        "timeLimitMinutes": 60,
    }
    if guest_limit is not None:
        item["authorizedGuestLimit"] = guest_limit
    if activated_at is not None:
        item["activatedAt"] = activated_at
    if expires_at is not None:
        item["expiresAt"] = expires_at
    if data_mb is not None:
        item["dataUsageLimitMBytes"] = data_mb
    if rx_kbps is not None:
        item["rxRateLimitKbps"] = rx_kbps
    if tx_kbps is not None:
        item["txRateLimitKbps"] = tx_kbps
    return item


def page(data: list[dict], *, offset: int = 0, total: int | None = None) -> dict:
    """Return the documented pagination envelope used by Network API tests."""

    return {
        "offset": offset,
        "limit": 1000,
        "count": len(data),
        "totalCount": len(data) if total is None else total,
        "data": data,
    }


def connected_client() -> UniFiClient:
    client = UniFiClient("controller.example.invalid")
    client._api_key = "synthetic-test-key"
    client.site_id = SITE_ID
    return client


def test_normalize_bare_controller_to_field_tested_official_root():
    assert normalize_api_root("controller.example.invalid") == (
        "https://controller.example.invalid" + OFFICIAL_LOCAL_API_PATH
    )


def test_normalize_preserves_explicit_v1_root():
    root = "https://controller.example.invalid/proxy/network/integration/v1"
    assert normalize_api_root(root + "/") == root


@pytest.mark.parametrize(
    "value",
    (
        "http://controller.example.invalid/proxy/network/integration/v1",
        "https://user:secret@controller.example.invalid/proxy/network/integration/v1",
        "https://controller.example.invalid/proxy/network/integration",
        "https://controller.example.invalid/proxy/network/integration/v1?x=1",
    ),
)
def test_normalize_rejects_unsafe_or_non_root_urls(value):
    with pytest.raises(ValueError):
        normalize_api_root(value)


def test_connect_validates_info_and_single_site(monkeypatch):
    client = UniFiClient("controller.example.invalid")
    calls = []

    def fake_request(method, path, payload=None, expected=(200,), **kwargs):
        calls.append((method, path))
        if path == "/info":
            return {"applicationVersion": "10.6.106"}
        return {
            "offset": 0,
            "limit": 200,
            "count": 1,
            "totalCount": 1,
            "data": [{"id": SITE_ID, "name": "Default"}],
        }

    monkeypatch.setattr(client, "_request", fake_request)
    result = client.connect("api-key-only-in-memory")

    assert result == {
        "applicationVersion": "10.6.106",
        "siteId": SITE_ID,
        "siteName": "Default",
    }
    assert client.connected is True
    assert calls == [("GET", "/info"), ("GET", "/sites?offset=0&limit=200")]


def test_connect_refuses_to_guess_between_multiple_sites(monkeypatch):
    client = UniFiClient("controller.example.invalid")

    def fake_request(method, path, payload=None, expected=(200,), **kwargs):
        if path == "/info":
            return {"applicationVersion": "10.6.106"}
        return {
            "offset": 0,
            "limit": 200,
            "count": 2,
            "totalCount": 2,
            "data": [
                {"id": SITE_ID, "name": "One"},
                {"id": "66666666-7777-8888-9999-000000000000", "name": "Two"},
            ],
        }

    monkeypatch.setattr(client, "_request", fake_request)
    with pytest.raises(UniFiApiError, match="più siti"):
        client.connect("temporary-key")

    # Failed connection must not keep credential-bearing state usable.
    assert client.connected is False
    assert client._api_key == ""


def test_list_vouchers_maps_official_fields_and_unlimited(monkeypatch):
    client = connected_client()
    item = voucher_json(
        guest_limit=None,
        guest_count=2,
        activated_at="2026-09-19T16:05:00Z",
        expires_at="2026-09-19T17:05:00Z",
        data_mb=20,
        rx_kbps=5000,
        tx_kbps=2000,
    )
    monkeypatch.setattr(client, "_request", lambda *a, **k: page([item]))

    voucher = client.list_vouchers()[0]

    assert voucher.code_formatted == "12345-67890"
    assert voucher.recipient == "TEST"
    assert voucher.quota == 0
    assert voucher.used == 2
    assert voucher.usage_label == "2 / Illimitato"
    assert voucher.status == "USED_MULTIPLE"
    assert voucher.data_mb == 20
    assert voucher.down_kbps == 5000
    assert voucher.up_kbps == 2000
    assert voucher.start_time == int(
        datetime(2026, 9, 19, 16, 5, tzinfo=timezone.utc).timestamp()
    )


def test_list_vouchers_maps_expired_state(monkeypatch):
    client = connected_client()
    monkeypatch.setattr(
        client,
        "_request",
        lambda *a, **k: page([voucher_json(expired=True)]),
    )
    assert client.list_vouchers()[0].status == "EXPIRED"


def test_list_vouchers_follows_offset_pagination(monkeypatch):
    client = connected_client()
    first = voucher_json(
        voucher_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        code="1111122222",
    )
    second = voucher_json(
        voucher_id="ffffffff-1111-2222-3333-444444444444",
        code="3333344444",
    )
    paths = []

    def fake_request(method, path, payload=None, expected=(200,), **kwargs):
        paths.append(path)
        if "offset=0" in path:
            return page([first], offset=0, total=2)
        return page([second], offset=1, total=2)

    monkeypatch.setattr(client, "_request", fake_request)
    vouchers = client.list_vouchers()

    assert [v.code for v in vouchers] == ["1111122222", "3333344444"]
    assert "offset=0&limit=1000" in paths[0]
    assert "offset=1&limit=1000" in paths[1]


def test_create_payload_maps_all_documented_limits(monkeypatch):
    client = connected_client()
    calls = []

    def fake_request(method, path, payload=None, expected=(200,), **kwargs):
        calls.append((method, path, payload, expected))
        return {
            "vouchers": [
                voucher_json(
                    name="TEST",
                    guest_limit=3,
                    data_mb=20,
                    rx_kbps=5000,
                    tx_kbps=2000,
                )
            ]
        }

    monkeypatch.setattr(client, "_request", fake_request)
    created = client.create_vouchers(
        "TEST",
        1,
        5,
        1,
        3,
        data_mb=20,
        down_mbps=5,
        up_mbps=2,
    )

    assert created[0].quota == 3
    assert calls[0] == (
        "POST",
        f"/sites/{SITE_ID}/hotspot/vouchers",
        {
            "count": 1,
            "name": "TEST",
            "timeLimitMinutes": 5,
            "authorizedGuestLimit": 3,
            "dataUsageLimitMBytes": 20,
            "rxRateLimitKbps": 5000,
            "txRateLimitKbps": 2000,
        },
        (201,),
    )


def test_create_unlimited_omits_authorized_guest_limit(monkeypatch):
    client = connected_client()
    payloads = []

    def fake_request(method, path, payload=None, expected=(200,), **kwargs):
        payloads.append(payload)
        return {"vouchers": [voucher_json(guest_limit=None)]}

    monkeypatch.setattr(client, "_request", fake_request)
    created = client.create_vouchers("TEST", 1, 15, 1, 0)

    assert "authorizedGuestLimit" not in payloads[0]
    assert created[0].quota == 0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    (
        ({"down_mbps": 101}, "Download"),
        ({"up_mbps": 101}, "Upload"),
        ({"data_mb": 1_048_577}, "Limite dati"),
    ),
)
def test_create_rejects_values_outside_documented_limits(kwargs, message):
    client = connected_client()
    with pytest.raises(UniFiApiError, match=message):
        client.create_vouchers("TEST", 1, 5, 1, 1, **kwargs)


def test_delete_multiple_uses_individual_uuid_endpoints(monkeypatch):
    client = connected_client()
    calls = []

    def fake_request(method, path, payload=None, expected=(200,), **kwargs):
        calls.append((method, path))
        return {"vouchersDeleted": 1}

    monkeypatch.setattr(client, "_request", fake_request)
    ids = [
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "ffffffff-1111-2222-3333-444444444444",
    ]
    client.delete_vouchers(ids)

    assert calls == [
        ("DELETE", f"/sites/{SITE_ID}/hotspot/vouchers/{ids[0]}"),
        ("DELETE", f"/sites/{SITE_ID}/hotspot/vouchers/{ids[1]}"),
    ]


def test_delete_reports_partial_completion(monkeypatch):
    client = connected_client()
    calls = 0

    def fake_request(method, path, payload=None, expected=(200,), **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"vouchersDeleted": 1}
        raise UniFiApiError("synthetic failure")

    monkeypatch.setattr(client, "_request", fake_request)
    with pytest.raises(UniFiApiError, match="Eliminati 1 voucher su 2"):
        client.delete_vouchers(
            [
                "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "ffffffff-1111-2222-3333-444444444444",
            ]
        )


def test_tls_verification_is_enabled_by_default():
    client = UniFiClient("controller.example.invalid")
    assert client.trusted_cert_sha256 == ""
    assert client._ssl_context.check_hostname is True
    assert client._ssl_context.verify_mode == ssl.CERT_REQUIRED


def test_pinned_certificate_uses_scoped_unverified_transport():
    fingerprint = "a" * 64
    client = UniFiClient(
        "controller.example.invalid",
        trusted_cert_sha256=fingerprint,
    )
    assert client._ssl_context.check_hostname is False
    assert client._ssl_context.verify_mode == ssl.CERT_NONE


def test_pinned_connection_accepts_matching_peer_before_http(monkeypatch):
    der = b"synthetic-certificate"
    import hashlib
    expected = hashlib.sha256(der).hexdigest()

    class DummySocket:
        def getpeercert(self, binary_form=False):
            assert binary_form is True
            return der

    def fake_parent_connect(self):
        self.sock = DummySocket()

    monkeypatch.setattr(
        "http.client.HTTPSConnection.connect",
        fake_parent_connect,
    )
    conn = _PinnedHTTPSConnection(
        "controller.example.invalid",
        expected_sha256=expected,
        context=ssl._create_unverified_context(),
    )
    conn.connect()


def test_pinned_connection_blocks_changed_peer_before_http(monkeypatch):
    class DummySocket:
        def getpeercert(self, binary_form=False):
            return b"different-certificate"

        def close(self):
            pass

    def fake_parent_connect(self):
        self.sock = DummySocket()

    monkeypatch.setattr(
        "http.client.HTTPSConnection.connect",
        fake_parent_connect,
    )
    conn = _PinnedHTTPSConnection(
        "controller.example.invalid",
        expected_sha256="a" * 64,
        context=ssl._create_unverified_context(),
    )
    with pytest.raises(PinnedCertificateMismatch) as excinfo:
        conn.connect()
    assert excinfo.value.expected_sha256 == "a" * 64
    assert excinfo.value.actual_sha256 != "a" * 64


def test_invalid_certificate_pin_is_rejected():
    with pytest.raises(ValueError, match="Impronta SHA-256"):
        UniFiClient(
            "controller.example.invalid",
            trusted_cert_sha256="not-a-fingerprint",
        )


def test_request_exposes_old_and_new_pin_without_sending_followup(monkeypatch):
    client = connected_client()
    client.trusted_cert_sha256 = "a" * 64

    class FailingOpener:
        def open(self, request, timeout=None):
            raise URLError(
                PinnedCertificateMismatch("a" * 64, "b" * 64)
            )

    client.opener = FailingOpener()

    with pytest.raises(UniFiCertificateChanged) as excinfo:
        client._request("GET", "/info")

    assert excinfo.value.previous_fingerprint == "a" * 64
    assert excinfo.value.fingerprint == "b" * 64


def test_redirects_are_rejected_before_followup_request():
    handler = _NoRedirectHandler()
    request = type("Req", (), {"full_url": "https://controller.invalid/v1/info"})()

    with pytest.raises(HTTPError, match="Redirect UniFi rifiutato"):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://other-host.invalid/steal",
        )


def test_create_rejects_quantity_above_ui_limit():
    client = connected_client()
    with pytest.raises(UniFiApiError, match="1 e 50"):
        client.create_vouchers("TEST", 51, 5, 1, 1)


def test_pinned_connection_missing_socket_raises_typed_mismatch(monkeypatch):
    def fake_parent_connect(self):
        self.sock = None

    monkeypatch.setattr(
        "http.client.HTTPSConnection.connect",
        fake_parent_connect,
    )
    conn = _PinnedHTTPSConnection(
        "controller.example.invalid",
        expected_sha256="a" * 64,
        context=ssl._create_unverified_context(),
    )
    with pytest.raises(PinnedCertificateMismatch) as excinfo:
        conn.connect()
    assert excinfo.value.expected_sha256 == "a" * 64
    assert excinfo.value.actual_sha256 == ""

def test_get_voucher_404_reports_missing_voucher_not_api_root():
    client = connected_client()

    class NotFoundOpener:
        def open(self, request, timeout=None):
            raise HTTPError(
                request.full_url,
                404,
                "Not Found",
                {},
                None,
            )

    client.opener = NotFoundOpener()

    with pytest.raises(UniFiApiError, match="non è più presente"):
        client.get_voucher("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


def test_generic_404_still_reports_api_root_configuration():
    client = connected_client()

    class NotFoundOpener:
        def open(self, request, timeout=None):
            raise HTTPError(
                request.full_url,
                404,
                "Not Found",
                {},
                None,
            )

    client.opener = NotFoundOpener()

    with pytest.raises(UniFiApiError, match="URL API"):
        client._request("GET", "/info")



def test_post_transport_failure_is_typed_as_uncertain_mutation():
    client = connected_client()

    class FailingOpener:
        def open(self, request, timeout=None):
            raise URLError(TimeoutError("synthetic timeout"))

    client.opener = FailingOpener()

    with pytest.raises(UniFiMutationUncertain, match="non ripeterla"):
        client._request(
            "POST",
            f"/sites/{SITE_ID}/hotspot/vouchers",
            {"count": 1},
            expected=(201,),
        )


def test_get_transport_failure_remains_safe_to_retry():
    client = connected_client()

    class FailingOpener:
        def open(self, request, timeout=None):
            raise URLError(TimeoutError("synthetic timeout"))

    client.opener = FailingOpener()

    with pytest.raises(UniFiApiError, match="non raggiungibile") as excinfo:
        client._request("GET", "/info")

    assert not isinstance(excinfo.value, UniFiMutationUncertain)


def test_post_server_error_is_conservatively_typed_as_uncertain():
    client = connected_client()

    class ServerErrorOpener:
        def open(self, request, timeout=None):
            raise HTTPError(
                request.full_url,
                503,
                "Service Unavailable",
                {},
                None,
            )

    client.opener = ServerErrorOpener()

    with pytest.raises(UniFiMutationUncertain):
        client._request(
            "POST",
            f"/sites/{SITE_ID}/hotspot/vouchers",
            {"count": 1},
            expected=(201,),
        )


def test_create_malformed_201_body_is_uncertain_not_safe_to_replay(monkeypatch):
    client = connected_client()
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: {})

    with pytest.raises(UniFiMutationUncertain):
        client.create_vouchers("TEST", 1, 5, 1, 1)


def test_create_wrong_201_voucher_count_is_uncertain(monkeypatch):
    client = connected_client()
    monkeypatch.setattr(
        client,
        "_request",
        lambda *args, **kwargs: {
            "vouchers": [
                voucher_json(
                    voucher_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    code="1111122222",
                ),
                voucher_json(
                    voucher_id="ffffffff-1111-2222-3333-444444444444",
                    code="3333344444",
                ),
            ]
        },
    )

    with pytest.raises(UniFiMutationUncertain):
        client.create_vouchers("TEST", 1, 5, 1, 1)
