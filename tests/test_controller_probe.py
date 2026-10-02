"""Tests for the onboarding UniFi probe credential boundary."""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime

from voucher_management.controller_probe import (
    ControllerProbeResult,
    probe_controller,
)


class FakeClient:
    def __init__(
        self,
        api_root,
        *,
        trusted_cert_sha256=None,
        preferred_site_id=None,
    ):
        self.base_url = api_root
        self.trusted_cert_sha256 = trusted_cert_sha256 or ""
        self.preferred_site_id = preferred_site_id or ""
        self.connected_key = None

    def connect(self, api_key):
        self.connected_key = api_key
        return {
            "applicationVersion": "10.6.106",
            "siteName": "Sala Assemblee",
        }

    def list_vouchers(self):
        return ["v1", "v2"]


def test_probe_keeps_api_key_only_inside_active_client_memory():
    result = probe_controller(
        "https://controller.example/proxy/network/integration/v1",
        "secret-api-key",
        client_factory=FakeClient,
    )

    assert result.client.connected_key == "secret-api-key"
    assert result.info["siteName"] == "Sala Assemblee"
    assert result.vouchers == ("v1", "v2")
    assert datetime.fromisoformat(result.observed_at).tzinfo is not None

    field_names = {field.name.lower() for field in fields(ControllerProbeResult)}
    assert "api_key" not in field_names
    assert "password" not in field_names
    assert "secret" not in field_names


def test_probe_passes_saved_site_uuid_as_nonsecret_identity():
    result = probe_controller(
        "https://controller.example/proxy/network/integration/v1",
        "memory-only-key",
        preferred_site_id="site-uuid",
        client_factory=FakeClient,
    )

    assert result.client.preferred_site_id == "site-uuid"


def test_probe_passes_only_certificate_pin_as_nonsecret_configuration():
    result = probe_controller(
        "https://controller.example/proxy/network/integration/v1",
        "memory-only-key",
        trusted_cert_sha256="a" * 64,
        client_factory=FakeClient,
    )

    assert result.client.trusted_cert_sha256 == "a" * 64
