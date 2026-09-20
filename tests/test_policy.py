from voucher_management.app import VoucherApp
from voucher_management.history import PrintStats
from voucher_management.policy import evaluate_delete_policy
from voucher_management.unifi_api import ApiVoucher


def voucher(**overrides):
    values = {
        "id": "test-id",
        "code": "1234567890",
        "recipient": "Test",
        "duration_minutes": 60,
        "create_time": 1,
        "quota": 1,
        "used": 0,
        "status": "VALID_MULTI",
    }
    values.update(overrides)
    return ApiVoucher(**values)


def test_unused_unprinted_voucher_can_be_deleted():
    assert evaluate_delete_policy(
        voucher(),
        PrintStats(),
    ).allowed is True


def test_printed_voucher_cannot_be_deleted():
    result = evaluate_delete_policy(
        voucher(),
        PrintStats(print_jobs=1, printed_copies=1),
    )
    assert result.allowed is False
    assert result.reason == "printed"


def test_used_voucher_cannot_be_deleted_even_without_local_print():
    result = evaluate_delete_policy(
        voucher(used=1, status="USED_MULTIPLE"),
        PrintStats(),
    )
    assert result.allowed is False
    assert result.reason == "in_use"


def test_expired_voucher_cannot_be_deleted():
    result = evaluate_delete_policy(
        voucher(status="EXPIRED"),
        PrintStats(),
    )
    assert result.allowed is False
    assert result.reason == "expired"


def test_ui_expiry_guard_uses_controller_state_not_translated_label():
    assert VoucherApp._is_expired(voucher(status="EXPIRED")) is True
    assert VoucherApp._is_expired(voucher(status="VALID_MULTI")) is False
