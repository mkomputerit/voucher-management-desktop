"""Tests for retention onboarding without requiring a real Tk display."""

from __future__ import annotations

from types import SimpleNamespace

from voucher_management import retention_ui
from voucher_management.history import HistoryError
from voucher_management.retention_ui import RetentionMixin


def test_retention_intro_is_skipped_after_installation_ack(monkeypatch):
    calls = []
    fake = SimpleNamespace(database=object())

    monkeypatch.setattr(
        retention_ui,
        "retention_policy_configured",
        lambda database: True,
    )
    monkeypatch.setattr(
        retention_ui,
        "retention_intro_seen",
        lambda database: True,
    )
    monkeypatch.setattr(
        retention_ui,
        "RetentionIntroDialog",
        lambda parent: (_ for _ in ()).throw(
            AssertionError("dialog must not reopen after acknowledgement")
        ),
    )

    RetentionMixin.show_retention_intro_if_needed(fake)

    assert calls == []


def test_retention_intro_continue_marks_installation_seen(monkeypatch):
    calls = []
    fake = SimpleNamespace(
        database=object(),
        open_retention_review=lambda: calls.append(("review",)),
    )

    monkeypatch.setattr(
        retention_ui,
        "retention_policy_configured",
        lambda database: True,
    )
    monkeypatch.setattr(
        retention_ui,
        "retention_intro_seen",
        lambda database: False,
    )
    monkeypatch.setattr(
        retention_ui,
        "mark_retention_intro_seen",
        lambda database, now: calls.append(("seen", database, now)),
    )
    monkeypatch.setattr(
        retention_ui,
        "RetentionIntroDialog",
        lambda parent: SimpleNamespace(result="continue"),
    )

    RetentionMixin.show_retention_intro_if_needed(fake)

    assert [entry[0] for entry in calls] == ["seen"]


def test_retention_intro_review_marks_seen_then_opens_advanced_review(monkeypatch):
    calls = []
    fake = SimpleNamespace(
        database=object(),
        open_retention_review=lambda: calls.append(("review",)),
    )

    monkeypatch.setattr(
        retention_ui,
        "retention_policy_configured",
        lambda database: True,
    )
    monkeypatch.setattr(
        retention_ui,
        "retention_intro_seen",
        lambda database: False,
    )
    monkeypatch.setattr(
        retention_ui,
        "mark_retention_intro_seen",
        lambda database, now: calls.append(("seen", database, now)),
    )
    monkeypatch.setattr(
        retention_ui,
        "RetentionIntroDialog",
        lambda parent: SimpleNamespace(result="review"),
    )

    RetentionMixin.show_retention_intro_if_needed(fake)

    assert [entry[0] for entry in calls] == ["seen", "review"]


def test_cancelled_retention_intro_is_not_acknowledged(monkeypatch):
    calls = []
    fake = SimpleNamespace(database=object())

    monkeypatch.setattr(
        retention_ui,
        "retention_policy_configured",
        lambda database: True,
    )
    monkeypatch.setattr(
        retention_ui,
        "retention_intro_seen",
        lambda database: False,
    )
    monkeypatch.setattr(
        retention_ui,
        "mark_retention_intro_seen",
        lambda database, now: calls.append(("seen", database, now)),
    )
    monkeypatch.setattr(
        retention_ui,
        "RetentionIntroDialog",
        lambda parent: SimpleNamespace(result=None),
    )

    RetentionMixin.show_retention_intro_if_needed(fake)

    assert calls == []



def test_unconfigured_policy_opens_review_instead_of_inventing_defaults(monkeypatch):
    calls = []
    fake = SimpleNamespace(
        database=object(),
        after_idle=lambda callback: calls.append(callback),
    )
    monkeypatch.setattr(
        retention_ui,
        "retention_policy_configured",
        lambda database: False,
    )

    RetentionMixin.show_retention_intro_if_needed(fake)

    assert len(calls) == 1


def test_security_revoke_ui_persists_intent_before_controller_delete(
    monkeypatch,
    tmp_path,
):
    order = []
    tasks = []
    remote = SimpleNamespace(id="remote-1")
    outcome = SimpleNamespace(
        vouchers=(),
        refresh_error=None,
    )

    class FakeDb:
        def __init__(self, path):
            order.append(("db-open", path))

        def initialize(self):
            order.append(("db-init",))

        def close(self):
            order.append(("db-close",))

    app = SimpleNamespace(
        active_controller_id=7,
        client=SimpleNamespace(
            list_vouchers=lambda: order.append(("list",)) or [remote],
        ),
        paths=SimpleNamespace(database=tmp_path / "voucher.db"),
        _windows_operator_identity=lambda: r"PC\operator",
        _run_network_task=lambda label, worker, success, error: (
            tasks.append((label, worker, success, error)) or True
        ),
        checked_ids={"remote-1"},
        vouchers=[remote],
        controller_snapshot_live=True,
        populate=lambda: order.append(("populate",)),
    )
    fake = SimpleNamespace(
        app=app,
        tree=SimpleNamespace(selection=lambda: ("11",)),
        _candidates={11: SimpleNamespace(voucher_id=11)},
        _now=lambda: "2026-09-30T10:00:00+00:00",
        _refresh=lambda: order.append(("dialog-refresh",)),
    )

    monkeypatch.setattr(
        retention_ui.messagebox,
        "askyesno",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        retention_ui.messagebox,
        "showinfo",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        retention_ui.messagebox,
        "showwarning",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(retention_ui, "Database", FakeDb)
    monkeypatch.setattr(
        retention_ui,
        "persist_refresh_snapshot_to_path",
        lambda *args, **kwargs: order.append(("persist-snapshot",)),
    )
    monkeypatch.setattr(
        retention_ui,
        "prepare_security_revocation_operation",
        lambda *args, **kwargs: (
            order.append(("prepare-intent",)) or ("remote-1",)
        ),
    )
    monkeypatch.setattr(
        retention_ui,
        "delete_vouchers_and_refresh",
        lambda *args, **kwargs: (
            order.append(("delete",)) or outcome
        ),
    )

    retention_ui.SecurityRevocationDialog._revoke_selected(fake)

    assert len(tasks) == 1
    result = tasks[0][1]()
    assert result is outcome
    assert order.index(("prepare-intent",)) < order.index(("delete",))
    assert order.count(("persist-snapshot",)) == 2


def test_retention_review_hides_candidates_when_history_is_unverifiable(monkeypatch):
    messages = []
    status_values = []
    tree = SimpleNamespace(
        get_children=lambda: (),
        delete=lambda *args: None,
    )
    fake = SimpleNamespace(
        tree=tree,
        app=SimpleNamespace(
            database=object(),
            history=object(),
            settings={},
        ),
        _now=lambda: "2026-09-27T08:00:00+00:00",
        status=SimpleNamespace(set=lambda value: status_values.append(value)),
        _candidates=None,
    )

    monkeypatch.setattr(
        retention_ui,
        "retention_policy_configured",
        lambda database: True,
    )
    monkeypatch.setattr(
        retention_ui,
        "reviewable_retention_candidates",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            HistoryError("corrupt")
        ),
    )
    monkeypatch.setattr(
        retention_ui.messagebox,
        "showerror",
        lambda *args, **kwargs: messages.append((args, kwargs)),
    )

    retention_ui.RetentionReviewDialog._refresh(fake)

    assert fake._candidates == {}
    assert status_values
    assert "non verificabile" in status_values[-1]
    assert messages
