from pathlib import Path

import pytest

from voucher_management.mutation_guard import (
    CreateMutationGuard,
    CreateMutationGuardError,
)


def test_create_guard_survives_new_instance_until_explicit_clear(tmp_path: Path):
    path = tmp_path / "pending_create_guard"
    first = CreateMutationGuard(path)

    first.begin()

    assert first.pending is True
    assert path.read_text(encoding="ascii") == "pending\n"

    restarted = CreateMutationGuard(path)
    assert restarted.pending is True

    assert restarted.clear() is True
    assert restarted.pending is False
    assert restarted.clear() is False


def test_create_guard_refuses_second_begin_without_clearing(tmp_path: Path):
    guard = CreateMutationGuard(tmp_path / "pending_create_guard")
    guard.begin()

    with pytest.raises(CreateMutationGuardError, match="sincronizzazione"):
        guard.begin()

    assert guard.pending is True
