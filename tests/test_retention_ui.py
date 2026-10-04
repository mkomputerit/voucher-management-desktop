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
        "ensure_retention_policy",
        lambda database, now: calls.append(("ensure", database, now)),
    )
    monkeypatch.setattr(
        retention_ui,
        "retention_intro_seen",
        lambda database: True,
    )
    monkeypatch.setattr(
        retention_ui,
        "retention_days_configured",
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

    assert calls and calls[0][0] == "ensure"


def test_retention_intro_continue_marks_seen_and_opens_required_review(monkeypatch):
    calls = []
    fake = SimpleNamespace(
        database=object(),
        open_retention_review=lambda: calls.append(("review",)),
    )

    monkeypatch.setattr(
        retention_ui,
        "ensure_retention_policy",
        lambda database, now: calls.append(("ensure", database, now)),
    )
    monkeypatch.setattr(
        retention_ui,
        "retention_intro_seen",
        lambda database: False,
    )
    monkeypatch.setattr(
        retention_ui,
        "retention_days_configured",
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

    assert [entry[0] for entry in calls] == ["ensure", "seen", "review"]


def test_retention_intro_review_marks_seen_then_opens_advanced_review(monkeypatch):
    calls = []
    fake = SimpleNamespace(
        database=object(),
        open_retention_review=lambda: calls.append(("review",)),
    )

    monkeypatch.setattr(
        retention_ui,
        "ensure_retention_policy",
        lambda database, now: calls.append(("ensure", database, now)),
    )
    monkeypatch.setattr(
        retention_ui,
        "retention_intro_seen",
        lambda database: False,
    )
    monkeypatch.setattr(
        retention_ui,
        "retention_days_configured",
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

    assert [entry[0] for entry in calls] == ["ensure", "seen", "review"]


def test_seen_intro_but_missing_threshold_still_opens_review(monkeypatch):
    calls = []
    fake = SimpleNamespace(
        database=object(),
        open_retention_review=lambda: calls.append(("review",)),
    )

    monkeypatch.setattr(
        retention_ui,
        "ensure_retention_policy",
        lambda database, now: calls.append(("ensure", database, now)),
    )
    monkeypatch.setattr(
        retention_ui,
        "retention_intro_seen",
        lambda database: True,
    )
    monkeypatch.setattr(
        retention_ui,
        "retention_days_configured",
        lambda database: False,
    )
    monkeypatch.setattr(
        retention_ui,
        "RetentionIntroDialog",
        lambda parent: (_ for _ in ()).throw(
            AssertionError("old intro must not reopen")
        ),
    )

    RetentionMixin.show_retention_intro_if_needed(fake)

    assert [entry[0] for entry in calls] == ["ensure", "review"]


def test_cancelled_retention_intro_is_not_acknowledged(monkeypatch):
    calls = []
    fake = SimpleNamespace(database=object())

    monkeypatch.setattr(
        retention_ui,
        "ensure_retention_policy",
        lambda database, now: calls.append(("ensure", database, now)),
    )
    monkeypatch.setattr(
        retention_ui,
        "retention_intro_seen",
        lambda database: False,
    )
    monkeypatch.setattr(
        retention_ui,
        "retention_days_configured",
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

    assert [entry[0] for entry in calls] == ["ensure"]



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
