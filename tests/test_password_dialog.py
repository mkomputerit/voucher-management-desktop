from __future__ import annotations

from types import SimpleNamespace

import pytest

from voucher_management.dialogs import (
    PasswordDialog,
    validate_password_entry,
)
from voucher_management import password_dialog


def password_value(marker: str = "p") -> str:
    return marker * 24


def test_password_validation_uses_crypto_policy_and_exact_text():
    value = "  " + password_value("x")
    assert validate_password_entry(value) == value


def test_password_confirmation_must_match_exactly():
    value = password_value("a")
    with pytest.raises(ValueError, match="non coincidono"):
        validate_password_entry(
            value,
            value + "x",
        )


def test_password_validation_rejects_short_value():
    with pytest.raises(ValueError, match="12"):
        validate_password_entry("too-short")


class _Entry:
    def __init__(self):
        self.values = []

    def configure(self, **kwargs):
        self.values.append(kwargs)


def test_password_dialog_toggle_controls_both_entries():
    first = _Entry()
    second = _Entry()
    fake = SimpleNamespace(
        show_var=SimpleNamespace(get=lambda: True),
        password_entry=first,
        confirm_entry=second,
    )

    PasswordDialog._toggle_visibility(fake)

    assert first.values[-1] == {"show": ""}
    assert second.values[-1] == {"show": ""}

    fake.show_var = SimpleNamespace(get=lambda: False)
    PasswordDialog._toggle_visibility(fake)

    assert first.values[-1] == {"show": "•"}
    assert second.values[-1] == {"show": "•"}


def test_password_dialog_toggle_handles_single_entry():
    first = _Entry()
    fake = SimpleNamespace(
        show_var=SimpleNamespace(get=lambda: True),
        password_entry=first,
        confirm_entry=None,
    )

    PasswordDialog._toggle_visibility(fake)

    assert first.values[-1] == {"show": ""}


def test_legacy_password_module_is_only_a_compatibility_reexport():
    assert password_dialog.PasswordDialog is PasswordDialog
    assert (
        password_dialog.validate_password_entry
        is validate_password_entry
    )
