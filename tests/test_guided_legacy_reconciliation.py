"""Tests for guided legacy reconciliation after authoritative UniFi sync."""

from __future__ import annotations

from types import SimpleNamespace

from voucher_management.data_maintenance_ui import DataMaintenanceMixin
from voucher_management import data_maintenance_ui


class _Logger:
    def __init__(self):
        self.events = []

    def info(self, *args, **kwargs):
        self.events.append(("info", args))

    def warning(self, *args, **kwargs):
        self.events.append(("warning", args))


def _immediate_background(_label, worker, success, failure, **_kwargs):
    try:
        success(worker())
    except Exception as exc:
        failure(exc)
    return True


def test_guided_reconciliation_runs_once_per_session_and_reuses_verified_plan(
    tmp_path,
    monkeypatch,
):
    history_path = tmp_path / "history.jsonl"
    history_path.write_text("{}\n", encoding="utf-8")
    plan = SimpleNamespace(
        total_rows=1,
        resolved=(object(),),
        ambiguous=(),
        unresolved=(),
    )
    planned = []
    launched = []

    monkeypatch.setattr(
        data_maintenance_ui,
        "legacy_candidates_from_database",
        lambda _db: ("candidate",),
    )
    monkeypatch.setattr(
        data_maintenance_ui,
        "build_legacy_migration_plan",
        lambda **kwargs: planned.append(kwargs) or plan,
    )
    monkeypatch.setattr(
        data_maintenance_ui,
        "legacy_migration_plan_needs_reconciliation",
        lambda _db, _plan: True,
    )

    fake = SimpleNamespace(
        paths=SimpleNamespace(history=history_path),
        history=SimpleNamespace(
            verified_identity_material=lambda: ("0123456789abcdef", "secret")
        ),
        database=object(),
        logger=_Logger(),
        _run_background_task=_immediate_background,
    )
    fake.migrate_legacy_history = lambda **kwargs: launched.append(kwargs)

    DataMaintenanceMixin.offer_legacy_reconciliation_after_sync(fake)
    DataMaintenanceMixin.offer_legacy_reconciliation_after_sync(fake)

    assert len(planned) == 1
    assert len(launched) == 1
    assert launched[0]["parent"] is fake
    assert launched[0]["prebuilt_plan"] is plan


def test_guided_reconciliation_stays_silent_when_plan_is_already_reconciled(
    tmp_path,
    monkeypatch,
):
    history_path = tmp_path / "history.jsonl"
    history_path.write_text("{}\n", encoding="utf-8")
    plan = SimpleNamespace(
        total_rows=1,
        resolved=(),
        ambiguous=(),
        unresolved=(object(),),
    )
    launched = []

    monkeypatch.setattr(
        data_maintenance_ui,
        "legacy_candidates_from_database",
        lambda _db: (),
    )
    monkeypatch.setattr(
        data_maintenance_ui,
        "build_legacy_migration_plan",
        lambda **_kwargs: plan,
    )
    monkeypatch.setattr(
        data_maintenance_ui,
        "legacy_migration_plan_needs_reconciliation",
        lambda _db, _plan: False,
    )

    fake = SimpleNamespace(
        paths=SimpleNamespace(history=history_path),
        history=SimpleNamespace(
            verified_identity_material=lambda: ("0123456789abcdef", "secret")
        ),
        database=object(),
        logger=_Logger(),
        _run_background_task=_immediate_background,
    )
    fake.migrate_legacy_history = lambda **kwargs: launched.append(kwargs)

    DataMaintenanceMixin.offer_legacy_reconciliation_after_sync(fake)

    assert launched == []
    assert fake._legacy_reconciliation_checked_this_session is True
