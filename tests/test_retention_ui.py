"""Tests for retention onboarding without requiring a real Tk display."""

from __future__ import annotations

from types import SimpleNamespace

from voucher_management import retention_ui
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
        "RetentionIntroDialog",
        lambda parent: (_ for _ in ()).throw(
            AssertionError("dialog must not reopen after acknowledgement")
        ),
    )

    RetentionMixin.show_retention_intro_if_needed(fake)

    assert calls and calls[0][0] == "ensure"


def test_retention_intro_continue_marks_installation_seen(monkeypatch):
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
        "mark_retention_intro_seen",
        lambda database, now: calls.append(("seen", database, now)),
    )
    monkeypatch.setattr(
        retention_ui,
        "RetentionIntroDialog",
        lambda parent: SimpleNamespace(result="continue"),
    )

    RetentionMixin.show_retention_intro_if_needed(fake)

    assert [entry[0] for entry in calls] == ["ensure", "seen"]


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
