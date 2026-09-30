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
