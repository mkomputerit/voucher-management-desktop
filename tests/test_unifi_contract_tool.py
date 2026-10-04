from voucher_management.unifi_api import ApiVoucher
from tools.verify_unifi_contract_live import build_contract_summary


def voucher(voucher_id: str, code: str, *, quota: int, used: int, status: str):
    return ApiVoucher(
        id=voucher_id,
        code=code,
        recipient="Sensitive Guest Name",
        duration_minutes=60,
        create_time=1,
        quota=quota,
        used=used,
        status=status,
    )


def test_contract_summary_is_privacy_safe_and_covers_usage_shapes():
    vouchers = [
        voucher("uuid-one", "1111122222", quota=1, used=0, status="VALID_MULTI"),
        voucher("uuid-two", "3333344444", quota=5, used=1, status="USED_MULTIPLE"),
        voucher("uuid-three", "5555566666", quota=0, used=7, status="USED_MULTIPLE"),
        voucher("uuid-four", "7777788888", quota=1, used=1, status="EXPIRED"),
    ]

    summary = build_contract_summary(
        {
            "applicationVersion": "10.6.106",
            "siteId": "real-site-uuid-must-not-be-output",
        },
        vouchers,
    )

    assert summary["network_version"] == "10.6.106"
    assert summary["voucher_count"] == 4
    assert summary["usage_shapes"] == {
        "multi": 1,
        "single": 2,
        "unlimited": 1,
    }
    assert summary["states"] == {
        "expired": 1,
        "unused": 1,
        "used": 2,
    }

    rendered = repr(summary)
    assert "real-site-uuid-must-not-be-output" not in rendered
    assert "Sensitive Guest Name" not in rendered
    assert "1111122222" not in rendered
    assert "uuid-one" not in rendered
