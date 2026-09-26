"""Tests for report credential-exposure policy."""

import pytest

from voucher_management.report_policy import (
    ReportPurpose,
    report_code_value,
    voucher_code_policy,
)


@pytest.mark.parametrize("purpose", [ReportPurpose.SUMMARY, ReportPurpose.AUDIT])
def test_historical_reports_never_expose_voucher_code(purpose):
    assert voucher_code_policy(purpose).expose_code is False
    assert voucher_code_policy(purpose, include_code_requested=True).expose_code is False
    assert report_code_value(
        "12345-67890",
        purpose,
        include_code_requested=True,
    ) == ""


def test_operational_handoff_hides_code_by_default():
    decision = voucher_code_policy(ReportPurpose.OPERATIONAL_HANDOFF)
    assert decision.expose_code is False
    assert decision.reason == "code_hidden_by_default"


def test_operational_handoff_requires_explicit_code_request():
    decision = voucher_code_policy(
        ReportPurpose.OPERATIONAL_HANDOFF,
        include_code_requested=True,
    )
    assert decision.expose_code is True
    assert decision.reason == "explicit_operational_handoff"
    assert report_code_value(
        "12345-67890",
        ReportPurpose.OPERATIONAL_HANDOFF,
        include_code_requested=True,
    ) == "12345-67890"
