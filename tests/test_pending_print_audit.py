from __future__ import annotations

from pathlib import Path

import pytest

from voucher_management.history import HistoryError, HistoryService
from voucher_management.security.history_key import HistoryKeyStore
from voucher_management.settings import SettingsStore


def make_history(tmp_path: Path):
    settings_store = SettingsStore(
        tmp_path / "config" / "settings.json"
    )
    history = HistoryService(
        tmp_path / "data" / "history.jsonl",
        tmp_path / "data" / "history.lock",
        settings_store,
        secret_store=HistoryKeyStore(tmp_path),
    )
    return settings_store, history


def test_pending_print_audit_survives_partial_append_and_repairs_once(
    tmp_path,
    monkeypatch,
):
    settings_store, history = make_history(tmp_path)
    settings = settings_store.load()
    codes = ["11111-22222", "33333-44444"]
    audit_id = "f" * 32
    submitted_at = "2026-09-21T20:00:00+00:00"
    original_append = history._append

    def partial_append(records):
        original_append(records[:1])
        raise HistoryError("synthetic crash after first row")

    monkeypatch.setattr(history, "_append", partial_append)

    with pytest.raises(HistoryError, match="synthetic crash"):
        history.record_print(
            codes,
            Path("Voucher_Crash.pdf"),
            1,
            settings,
            audit_id=audit_id,
            submitted_at=submitted_at,
        )

    assert history.pending_print_path.exists()
    pending_text = history.pending_print_path.read_text(
        encoding="utf-8"
    )
    assert codes[0] not in pending_text
    assert codes[1] not in pending_text
    assert audit_id in pending_text

    # Simulate a fresh process after the interruption.
    recovered = HistoryService(
        history.history_path,
        history.lock_path,
        settings_store,
        secret_store=HistoryKeyStore(tmp_path),
    )
    assert recovered.recover_pending_print_audit() is True
    assert not recovered.pending_print_path.exists()

    stats = recovered.stats_for_codes(
        codes,
        settings_store.load(),
    )
    assert stats[codes[0]].print_jobs == 1
    assert stats[codes[1]].print_jobs == 1

    # A second startup/recovery is a no-op, not a duplicate.
    assert recovered.recover_pending_print_audit() is False
    stats = recovered.stats_for_codes(
        codes,
        settings_store.load(),
    )
    assert stats[codes[0]].print_jobs == 1
    assert stats[codes[1]].print_jobs == 1


def test_new_print_is_blocked_while_pending_audit_exists(tmp_path):
    settings_store, history = make_history(tmp_path)
    settings = settings_store.load()
    audit_id = "a" * 32

    records = [
        {
            "event": "print",
            "voucher_id": history._digest(
                "12345-67890",
                history._secret(settings),
            ),
            "timestamp": "2026-09-21T20:10:00+00:00",
            "output_file": "Voucher_Pending.pdf",
            "document_copies": 1,
            "physical_copies": 1,
            "print_job_id": audit_id,
        }
    ]
    history._persist_pending_print(records, audit_id)

    with pytest.raises(HistoryError, match="stampa precedente"):
        history.assert_no_pending_print_audit()


def test_pending_print_descriptor_rejects_different_job(tmp_path):
    settings_store, history = make_history(tmp_path)
    settings = settings_store.load()
    key = history._secret(settings)

    first = [
        {
            "event": "print",
            "voucher_id": history._digest("12345-67890", key),
            "timestamp": "2026-09-21T20:15:00+00:00",
            "output_file": "Voucher_A.pdf",
            "document_copies": 1,
            "physical_copies": 1,
            "print_job_id": "b" * 32,
        }
    ]
    second = [
        {
            "event": "print",
            "voucher_id": history._digest("33333-44444", key),
            "timestamp": "2026-09-21T20:16:00+00:00",
            "output_file": "Voucher_B.pdf",
            "document_copies": 1,
            "physical_copies": 1,
            "print_job_id": "c" * 32,
        }
    ]

    history._persist_pending_print(first, "b" * 32)
    with pytest.raises(
        HistoryError,
        match="diversa registrazione stampa pendente",
    ):
        history._persist_pending_print(second, "c" * 32)


def test_prepared_print_requires_explicit_operator_confirmation(tmp_path):
    settings_store, history = make_history(tmp_path)
    settings = settings_store.load()
    code = "77777-88888"
    audit_id = "d" * 32
    submitted_at = "2026-09-21T21:00:00+00:00"

    history.prepare_print_audit(
        [code],
        Path("Voucher_Ambiguous.pdf"),
        1,
        settings,
        audit_id=audit_id,
        submitted_at=submitted_at,
    )

    assert history.pending_print_state() == "prepared"
    with pytest.raises(HistoryError, match="esito della stampa pendente"):
        history.recover_pending_print_audit()

    assert history.recover_pending_print_audit(
        assume_submitted=True
    ) is True
    assert history.pending_print_state() == ""
    stats = history.stats_for_codes(
        [code],
        settings_store.load(),
    )[code]
    assert stats.print_jobs == 1
    assert stats.printed_copies == 1


def test_prepared_print_can_be_discarded_when_operator_confirms_not_printed(
    tmp_path,
):
    settings_store, history = make_history(tmp_path)
    settings = settings_store.load()
    code = "99999-00000"

    history.prepare_print_audit(
        [code],
        Path("Voucher_Not_Printed.pdf"),
        1,
        settings,
        audit_id="e" * 32,
        submitted_at="2026-09-21T21:05:00+00:00",
    )

    assert history.pending_print_state() == "prepared"
    assert history.discard_prepared_print_audit() is True
    assert history.pending_print_state() == ""
    stats = history.stats_for_codes(
        [code],
        settings_store.load(),
    )[code]
    assert stats.print_jobs == 0
    assert stats.printed_copies == 0
