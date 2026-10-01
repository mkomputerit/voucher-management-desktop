"""Regression tests for lifecycle policy summary in Settings."""

from types import SimpleNamespace

from voucher_management.modern_app import ModernVoucherApp


class _Value:
    def __init__(self):
        self.value = None

    def set(self, value):
        self.value = value


class _Policy(dict):
    def keys(self):
        return super().keys()


def _fake(policy):
    return SimpleNamespace(
        database=SimpleNamespace(retention_policy=lambda: policy),
        settings_retention_summary_var=_Value(),
    )


def test_settings_summary_handles_unconfigured_migrated_policy():
    fake = _fake(
        _Policy(
            configured=0,
            unused_unprinted_days=None,
            printed_unused_revoke_days=None,
        )
    )

    ModernVoucherApp._refresh_retention_summary(fake)

    assert "da configurare" in fake.settings_retention_summary_var.value.lower()
    assert "revoca" in fake.settings_retention_summary_var.value.lower()


def test_settings_summary_shows_both_explicit_lifecycle_thresholds():
    fake = _fake(
        _Policy(
            configured=1,
            unused_unprinted_days=180,
            printed_unused_revoke_days=30,
        )
    )

    ModernVoucherApp._refresh_retention_summary(fake)

    assert "180 giorni" in fake.settings_retention_summary_var.value
    assert "30 giorni" in fake.settings_retention_summary_var.value
    assert "Revoca" in fake.settings_retention_summary_var.value
