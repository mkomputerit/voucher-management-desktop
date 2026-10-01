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


def test_create_guard_can_store_privacy_safe_confirmed_reporting_recovery(
    tmp_path: Path,
):
    from voucher_management.create_reporting_recovery import (
        load_pending_create_reporting,
    )

    path = tmp_path / "pending_create_guard"
    guard = CreateMutationGuard(path)
    guard.begin()

    guard.store_reporting_recovery(
        controller_id=7,
        voucher_ids=["uuid-1", "uuid-2"],
        is_nominal=True,
        confirmed_at="2026-09-30T14:20:00+00:00",
    )

    assert guard.pending is True
    assert guard.has_reporting_recovery is True
    pending = load_pending_create_reporting(path)
    assert pending is not None
    assert pending.controller_id == 7
    assert pending.voucher_ids == ("uuid-1", "uuid-2")
    assert pending.is_nominal is True

    serialized = path.read_text(encoding="utf-8").lower()
    assert "recipient" not in serialized
    assert "code" not in serialized
    assert "api" not in serialized


def test_corrupted_reporting_recovery_fails_closed(tmp_path: Path):
    path = tmp_path / "pending_create_guard"
    guard = CreateMutationGuard(path)
    guard.begin()
    path.write_text('{"format":1,"controller_id":7', encoding="utf-8")

    with pytest.raises(
        CreateMutationGuardError,
        match="Recovery della creazione confermata danneggiato",
    ):
        _ = guard.has_reporting_recovery

    assert guard.pending is True


def test_structurally_invalid_reporting_recovery_fails_closed(tmp_path: Path):
    path = tmp_path / "pending_create_guard"
    guard = CreateMutationGuard(path)
    guard.begin()
    path.write_text(
        '{"format":1,"controller_id":7,"voucher_ids":[],"is_nominal":true,'
        '"confirmed_at":"2026-09-30T14:30:00+00:00"}',
        encoding="utf-8",
    )

    with pytest.raises(
        CreateMutationGuardError,
        match="Recovery della creazione confermata non valido",
    ):
        _ = guard.has_reporting_recovery

    assert guard.pending is True
