from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from voucher_management.backup_crypto import decrypt_backup_file
from voucher_management.history import HistoryError, HistoryService
from voucher_management.history_exchange import (
    HISTORY_NAME,
    IDENTITY_NAME,
    MANIFEST_NAME,
    HistoryExchangeError,
    HistoryExchangeService,
)
from voucher_management.models import VoucherBatch, VoucherRecord
from voucher_management.security.history_key import HistoryKeyStore
from voucher_management.settings import SettingsStore


def exchange_password(marker: str = "x") -> str:
    return marker * 24


def make_history(tmp_path: Path, name: str, secret: str | None = None):
    root = tmp_path / name
    settings_store = SettingsStore(root / "config" / "settings.json")
    key_store = HistoryKeyStore(root)
    history = HistoryService(
        root / "data" / "history.jsonl",
        root / "data" / "history.lock",
        settings_store,
        secret_store=key_store,
    )
    if secret is not None:
        history.configure_secret(
            secret,
            settings_store.load(),
            replace=True,
        )
    return root, settings_store, history


def add_generated(
    history: HistoryService,
    settings_store: SettingsStore,
    *,
    code: str,
    recipient: str,
    repeats: int = 1,
    output: str = "Voucher_Test.pdf",
):
    batch = VoucherBatch(
        Path("source.pdf"),
        [
            VoucherRecord(
                code,
                duration_minutes=60,
                recipient=recipient,
            )
            for _ in range(repeats)
        ],
        recipient=recipient,
    )
    history.record_batch(
        batch,
        history.history_path.parent.parent / "Print" / output,
        settings_store.load(),
        reprint=False,
    )


def test_history_exchange_same_identity_merges_and_reimport_is_idempotent(
    tmp_path,
):
    history_key = "same-history-key-for-workstations"
    _, source_settings, source = make_history(
        tmp_path,
        "source",
        history_key,
    )
    _, target_settings, target = make_history(
        tmp_path,
        "target",
        history_key,
    )
    add_generated(
        source,
        source_settings,
        code="11111-22222",
        recipient="Source",
    )
    source.record_print(
        ["11111-22222"],
        Path("Voucher_Source.pdf"),
        2,
        source_settings.load(),
        audit_id="a" * 32,
        submitted_at="2026-09-21T18:00:00+00:00",
    )

    package = tmp_path / "history.vmhx"
    HistoryExchangeService(source).export(package, exchange_password())

    exchange = HistoryExchangeService(target)
    first = exchange.prepare_import(package, exchange_password())
    assert first.identity_relation == "same_identity"
    assert first.can_adopt_identity is False
    assert first.incoming_events == 2
    assert first.new_events == 2
    assert first.duplicate_events == 0

    assert exchange.apply_import(first) == 2

    second = exchange.prepare_import(package, exchange_password())
    assert second.new_events == 0
    assert second.duplicate_events == 2
    assert exchange.apply_import(second) == 0

    stats = target.stats_for_codes(
        ["11111-22222"],
        target_settings.load(),
    )["11111-22222"]
    assert stats.generated_documents == 1
    assert stats.print_jobs == 1
    assert stats.printed_copies == 2


def test_history_exchange_multiset_preserves_identical_generate_multiplicity(
    tmp_path,
):
    history_key = "shared-multiset-history-key"
    _, source_settings, source = make_history(
        tmp_path,
        "source",
        history_key,
    )
    target_root, target_settings, target = make_history(
        tmp_path,
        "target",
        history_key,
    )
    add_generated(
        source,
        source_settings,
        code="33333-44444",
        recipient="Repeated",
        repeats=3,
    )

    source_rows = list(source._items())
    assert len(source_rows) == 3

    # Simulate pre-event_id history so this test keeps exercising the legacy
    # multiset path after modern generate rows gained stable identities.
    legacy_rows = []
    for row in source_rows:
        legacy = dict(row)
        legacy.pop("event_id", None)
        legacy_rows.append(legacy)
    source.history_path.write_text(
        "".join(
            json.dumps(
                row,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
            for row in legacy_rows
        ),
        encoding="utf-8",
    )
    target.history_path.parent.mkdir(parents=True, exist_ok=True)
    target.history_path.write_text(
        json.dumps(
            legacy_rows[0],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )

    package = tmp_path / "multiplicity.vmhx"
    HistoryExchangeService(source).export(package, exchange_password())
    exchange = HistoryExchangeService(target)
    plan = exchange.prepare_import(package, exchange_password())

    assert plan.incoming_events == 3
    assert plan.duplicate_events == 1
    assert plan.new_events == 2
    assert exchange.apply_import(plan) == 2

    rows = list(target._items())
    assert len(rows) == 3
    stats = target.stats_for_codes(
        ["33333-44444"],
        target_settings.load(),
    )["33333-44444"]
    assert stats.generated_copies == 3


def test_identical_generate_events_from_two_workstations_do_not_collapse(
    tmp_path,
):
    history_key = "shared-identical-generate-key"
    _, a_settings, station_a = make_history(
        tmp_path,
        "station-a-identical",
        history_key,
    )
    _, b_settings, station_b = make_history(
        tmp_path,
        "station-b-identical",
        history_key,
    )

    for history, settings in (
        (station_a, a_settings),
        (station_b, b_settings),
    ):
        add_generated(
            history,
            settings,
            code="45454-56565",
            recipient="Same",
            output="Voucher_Same.pdf",
        )

    # Force every legacy-visible field to be identical. event_id must be the
    # only distinction that preserves the two independent workstation events.
    fixed_timestamp = "2026-09-22T06:00:00+00:00"
    for history in (station_a, station_b):
        rows = list(history._items())
        assert len(rows) == 1
        rows[0]["timestamp"] = fixed_timestamp
        history.history_path.write_text(
            json.dumps(
                rows[0],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )

    row_a = list(station_a._items())[0]
    row_b = list(station_b._items())[0]
    assert row_a["event_id"] != row_b["event_id"]

    package_a = tmp_path / "identical-a.vmhx"
    HistoryExchangeService(station_a).export(
        package_a,
        exchange_password("i"),
    )
    service_b = HistoryExchangeService(station_b)
    plan_b = service_b.prepare_import(
        package_a,
        exchange_password("i"),
    )

    assert plan_b.new_events == 1
    assert plan_b.duplicate_events == 0
    assert plan_b.conflict_events == 0
    assert service_b.apply_import(plan_b) == 1
    assert len(list(station_b._items())) == 2

    package_b = tmp_path / "identical-b.vmhx"
    service_b.export(package_b, exchange_password("j"))
    service_a = HistoryExchangeService(station_a)
    plan_a = service_a.prepare_import(
        package_b,
        exchange_password("j"),
    )
    assert plan_a.new_events == 1
    assert plan_a.duplicate_events == 1
    assert service_a.apply_import(plan_a) == 1

    assert sorted(
        row["event_id"] for row in station_a._items()
    ) == sorted(
        row["event_id"] for row in station_b._items()
    )


def test_history_exchange_rejects_print_job_conflict(tmp_path):
    history_key = "shared-print-conflict-key"
    _, source_settings, source = make_history(
        tmp_path,
        "source",
        history_key,
    )
    _, target_settings, target = make_history(
        tmp_path,
        "target",
        history_key,
    )
    job_id = "b" * 32
    submitted = "2026-09-21T18:05:00+00:00"

    source.record_print(
        ["55555-66666"],
        Path("Voucher_Print.pdf"),
        1,
        source_settings.load(),
        audit_id=job_id,
        submitted_at=submitted,
    )
    target.record_print(
        ["55555-66666"],
        Path("Voucher_Print.pdf"),
        2,
        target_settings.load(),
        audit_id=job_id,
        submitted_at=submitted,
    )

    package = tmp_path / "conflict.vmhx"
    HistoryExchangeService(source).export(package, exchange_password())

    exchange = HistoryExchangeService(target)
    plan = exchange.prepare_import(
        package,
        exchange_password(),
    )
    assert plan.conflict_events == 1
    assert plan.new_events == 0
    assert plan.duplicate_events == 0

    with pytest.raises(HistoryExchangeError, match="conflitti"):
        exchange.apply_import(plan)


def test_history_exchange_rejects_different_identity_when_local_has_rows(
    tmp_path,
):
    _, source_settings, source = make_history(
        tmp_path,
        "source",
        "source-history-secret-value",
    )
    _, target_settings, target = make_history(
        tmp_path,
        "target",
        "different-target-history-secret",
    )
    add_generated(
        source,
        source_settings,
        code="77777-88888",
        recipient="Source",
    )
    add_generated(
        target,
        target_settings,
        code="99999-00000",
        recipient="Target",
    )

    package = tmp_path / "different.vmhx"
    HistoryExchangeService(source).export(package, exchange_password())

    with pytest.raises(
        HistoryExchangeError,
        match="diversa identità",
    ):
        HistoryExchangeService(target).prepare_import(
            package,
            exchange_password(),
        )


def test_empty_workstation_requires_explicit_identity_adoption(tmp_path):
    source_key = "source-empty-adoption-secret"
    _, source_settings, source = make_history(
        tmp_path,
        "source",
        source_key,
    )
    _, target_settings, target = make_history(
        tmp_path,
        "target",
    )
    original_target_key = target.secret_store.get()
    original_target_fp = target_settings.load()[
        "history_key_fingerprint"
    ]
    add_generated(
        source,
        source_settings,
        code="12121-34343",
        recipient="Adopt",
    )

    package = tmp_path / "adopt.vmhx"
    HistoryExchangeService(source).export(package, exchange_password())
    exchange = HistoryExchangeService(target)
    plan = exchange.prepare_import(package, exchange_password())

    assert plan.can_adopt_identity is True
    assert plan.identity_relation == "empty_local_identity"

    with pytest.raises(
        HistoryExchangeError,
        match="adozione esplicita",
    ):
        exchange.apply_import(plan)

    assert target.secret_store.get() == original_target_key
    assert (
        target_settings.load()["history_key_fingerprint"]
        == original_target_fp
    )

    assert exchange.apply_import(plan, adopt_identity=True) == 1
    assert target.secret_store.get() == source_key
    assert (
        target_settings.load()["history_key_fingerprint"]
        == source_settings.load()["history_key_fingerprint"]
    )

    stats = target.stats_for_codes(
        ["12121-34343"],
        target_settings.load(),
    )["12121-34343"]
    assert stats.generated_documents == 1


def test_identity_adoption_rolls_back_if_history_replace_fails(
    tmp_path,
    monkeypatch,
):
    source_key = "source-rollback-history-secret"
    _, source_settings, source = make_history(
        tmp_path,
        "source",
        source_key,
    )
    _, target_settings, target = make_history(
        tmp_path,
        "target",
    )
    add_generated(
        source,
        source_settings,
        code="45454-56565",
        recipient="Rollback",
    )
    old_key = target.secret_store.get()
    old_settings = target_settings.load()

    package = tmp_path / "rollback.vmhx"
    HistoryExchangeService(source).export(package, exchange_password())
    exchange = HistoryExchangeService(target)
    plan = exchange.prepare_import(package, exchange_password())

    monkeypatch.setattr(
        exchange,
        "_replace_history",
        lambda _rows: (_ for _ in ()).throw(
            OSError("synthetic disk failure")
        ),
    )

    with pytest.raises(HistoryExchangeError):
        exchange.apply_import(plan, adopt_identity=True)

    assert target.secret_store.get() == old_key
    assert target_settings.load() == old_settings
    assert not target.history_path.exists()


def test_wrong_password_does_not_expose_or_merge_history(tmp_path):
    history_key = "same-password-failure-key"
    _, source_settings, source = make_history(
        tmp_path,
        "source",
        history_key,
    )
    _, _, target = make_history(
        tmp_path,
        "target",
        history_key,
    )
    add_generated(
        source,
        source_settings,
        code="67676-78787",
        recipient="Protected",
    )

    package = tmp_path / "protected.vmhx"
    HistoryExchangeService(source).export(package, exchange_password())

    with pytest.raises(
        HistoryExchangeError,
        match="Password non valida oppure pacchetto storico alterato",
    ):
        HistoryExchangeService(target).prepare_import(
            package,
            exchange_password("y"),
        )
    assert not target.history_path.exists()


def test_exchange_contains_only_manifest_history_and_identity(tmp_path):
    _, settings, history = make_history(
        tmp_path,
        "source",
        "minimal-exchange-history-secret",
    )
    add_generated(
        history,
        settings,
        code="89898-90909",
        recipient="Minimal",
    )

    package = tmp_path / "minimal.vmhx"
    HistoryExchangeService(history).export(package, exchange_password())
    decrypted = tmp_path / "exchange.zip"
    decrypt_backup_file(package, decrypted, exchange_password())

    with zipfile.ZipFile(decrypted, "r") as archive:
        assert set(archive.namelist()) == {
            MANIFEST_NAME,
            HISTORY_NAME,
            IDENTITY_NAME,
        }


def test_export_rejects_destination_inside_application_data(tmp_path):
    root, settings, history = make_history(
        tmp_path,
        "source",
        "inside-root-history-secret",
    )
    add_generated(
        history,
        settings,
        code="10101-20202",
        recipient="Inside",
    )

    with pytest.raises(
        HistoryExchangeError,
        match="fuori dalla cartella dati",
    ):
        HistoryExchangeService(history).export(
            root / "data" / "history.vmhx",
            exchange_password(),
        )


def test_two_divergent_workstations_converge_after_bidirectional_merge(
    tmp_path,
):
    history_key = "shared-convergence-history-key"
    _, a_settings, station_a = make_history(
        tmp_path,
        "station-a",
        history_key,
    )
    _, b_settings, station_b = make_history(
        tmp_path,
        "station-b",
        history_key,
    )

    add_generated(
        station_a,
        a_settings,
        code="11111-22222",
        recipient="Station A",
    )
    add_generated(
        station_b,
        b_settings,
        code="33333-44444",
        recipient="Station B",
    )

    package_a = tmp_path / "station-a.vmhx"
    HistoryExchangeService(station_a).export(
        package_a,
        exchange_password("a"),
    )
    service_b = HistoryExchangeService(station_b)
    plan_b = service_b.prepare_import(
        package_a,
        exchange_password("a"),
    )
    assert plan_b.new_events == 1
    assert plan_b.conflict_events == 0
    assert service_b.apply_import(plan_b) == 1

    package_b = tmp_path / "station-b.vmhx"
    service_b.export(
        package_b,
        exchange_password("b"),
    )
    service_a = HistoryExchangeService(station_a)
    plan_a = service_a.prepare_import(
        package_b,
        exchange_password("b"),
    )
    assert plan_a.new_events == 1
    assert plan_a.duplicate_events == 1
    assert plan_a.conflict_events == 0
    assert service_a.apply_import(plan_a) == 1

    def canonical_rows(history):
        return sorted(
            json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for row in history._items()
        )

    assert canonical_rows(station_a) == canonical_rows(station_b)

    # Once converged, exchanging the same merged package again is a no-op.
    final_plan = service_a.prepare_import(
        package_b,
        exchange_password("b"),
    )
    assert final_plan.new_events == 0
    assert final_plan.duplicate_events == 2
    assert final_plan.conflict_events == 0


def test_history_exchange_is_blocked_while_print_audit_is_pending(
    tmp_path,
    monkeypatch,
):
    _, settings, history = make_history(
        tmp_path,
        "source",
        "pending-exchange-history-key",
    )
    add_generated(
        history,
        settings,
        code="56565-67676",
        recipient="Pending",
    )
    monkeypatch.setattr(
        history,
        "assert_no_pending_print_audit",
        lambda: (_ for _ in ()).throw(
            HistoryError("pending print audit")
        ),
    )

    with pytest.raises(HistoryExchangeError, match="pending print audit"):
        HistoryExchangeService(history).export(
            tmp_path / "blocked.vmhx",
            exchange_password(),
        )


def test_history_exchange_rejects_invalid_generate_event_id(tmp_path):
    _, settings, history = make_history(
        tmp_path,
        "bad-event-id",
        "bad-event-id-shared-secret",
    )
    add_generated(
        history,
        settings,
        code="78787-89898",
        recipient="Bad Event",
    )
    rows = list(history._items())
    rows[0]["event_id"] = "not-valid!"
    history.history_path.write_text(
        json.dumps(
            rows[0],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        HistoryExchangeError,
        match="Identificativo evento non valido",
    ):
        HistoryExchangeService(history).export(
            tmp_path / "bad-event-id.vmhx",
            exchange_password(),
        )


def test_empty_second_station_adopts_then_both_diverge_and_converge(
    tmp_path,
):
    """Exercise the full manual two-workstation lifecycle in isolated roots."""

    source_key = "shared-two-station-lifecycle-key"
    _, a_settings, station_a = make_history(
        tmp_path,
        "installation-a",
        source_key,
    )
    _, b_settings, station_b = make_history(
        tmp_path,
        "installation-b",
    )

    add_generated(
        station_a,
        a_settings,
        code="10101-30303",
        recipient="Bootstrap A",
    )

    bootstrap = tmp_path / "bootstrap-a.vmhx"
    service_a = HistoryExchangeService(station_a)
    service_b = HistoryExchangeService(station_b)
    service_a.export(bootstrap, exchange_password("a"))

    bootstrap_plan = service_b.prepare_import(
        bootstrap,
        exchange_password("a"),
    )
    assert bootstrap_plan.can_adopt_identity is True
    assert bootstrap_plan.identity_relation == "empty_local_identity"
    assert bootstrap_plan.new_events == 1
    assert service_b.apply_import(
        bootstrap_plan,
        adopt_identity=True,
    ) == 1
    assert (
        b_settings.load()["history_key_fingerprint"]
        == a_settings.load()["history_key_fingerprint"]
    )

    # The installations now share an audit identity but evolve independently.
    add_generated(
        station_a,
        a_settings,
        code="20202-40404",
        recipient="Only A",
    )
    add_generated(
        station_b,
        b_settings,
        code="30303-50505",
        recipient="Only B",
    )

    from_a = tmp_path / "from-a.vmhx"
    service_a.export(from_a, exchange_password("b"))
    plan_b = service_b.prepare_import(
        from_a,
        exchange_password("b"),
    )
    assert plan_b.new_events == 1
    assert plan_b.duplicate_events == 1
    assert plan_b.conflict_events == 0
    assert service_b.apply_import(plan_b) == 1

    from_b = tmp_path / "from-b.vmhx"
    service_b.export(from_b, exchange_password("c"))
    plan_a = service_a.prepare_import(
        from_b,
        exchange_password("c"),
    )
    assert plan_a.new_events == 1
    assert plan_a.duplicate_events == 2
    assert plan_a.conflict_events == 0
    assert service_a.apply_import(plan_a) == 1

    def canonical_rows(history):
        return sorted(
            json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for row in history._items()
        )

    assert canonical_rows(station_a) == canonical_rows(station_b)

    # Re-importing an already merged package remains idempotent.
    final_plan = service_a.prepare_import(
        from_b,
        exchange_password("c"),
    )
    assert final_plan.new_events == 0
    assert final_plan.duplicate_events == 3
    assert final_plan.conflict_events == 0
