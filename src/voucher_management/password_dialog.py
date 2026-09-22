"""Compatibility re-exports for the consolidated dialog module."""

from .dialogs import (
    PasswordDialog,
    ask_password,
    validate_password_entry,
)

__all__ = [
    "PasswordDialog",
    "ask_password",
    "validate_password_entry",
]
