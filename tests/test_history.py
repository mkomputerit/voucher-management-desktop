from voucher_management.history import HistoryError, HistoryService
from voucher_management.models import VoucherBatch, VoucherRecord
from voucher_management.security.history_key import HistoryKeyStore
from voucher_management.settings import SettingsStore


def make_history(tmp_path):
    settings_store = SettingsStore(tmp_path / "config" / "settings.json")
    secret_store = HistoryKeyStore(tmp_path / "local")
    history = HistoryService(
        tmp_path / "data" / "history.jsonl",
        tmp_path / "data" / "history.lock",
        settings_store,
        secret_store=secret_store,
    )
    return settings_store, history


def test_history_has_no_plaintext_code(tmp_path):
    settings_store, history = make_history(tmp_path)
    settings = settings_store.load()
    # Controlled test setup: replace the automatically generated empty-history
    # key with a known value so the HMAC behaviour is deterministic.
    history.configure_secret("this-is-a-shared-history-secret", settings, replace=True)
    settings = settings_store.load()
    batch = VoucherBatch(tmp_path / "source.pdf", [VoucherRecord("93256-35147", 1)], recipient="TEST01")
    history.record_batch(batch, tmp_path / "Voucher_TEST01.pdf", settings, reprint=False)
    raw = (tmp_path / "data" / "history.jsonl").read_text(encoding="utf-8")
    assert "93256-35147" not in raw
    hits = history.find_duplicates(["93256-35147"], settings)
    assert hits["93256-35147"].recipient == "TEST01"


def test_missing_local_key_is_recovered_and_new_events_are_counted(tmp_path):
    settings_store, history = make_history(tmp_path)
    settings = settings_store.load()
    settings["history_key_fingerprint"] = "deadbeefdeadbeef"
    settings_store.save(settings)
    history.secret_store.clear()

    history.ensure_ready()
    repaired = settings_store.load()
    assert repaired["history_key_fingerprint"] != "deadbeefdeadbeef"
    assert history.secret_store.get()

    code = "12345-67890"
    output = tmp_path / "Print" / "Voucher_Test.pdf"
    batch = VoucherBatch(tmp_path / "source.pdf", [VoucherRecord(code, duration_minutes=60, recipient="Test")], recipient="Test")
    history.record_batch(batch, output, repaired, reprint=False)
    history.record_print([code], output, 1, repaired)

    stats = history.stats_for_codes([code], repaired)[code]
    assert stats.generated_documents == 1
    assert stats.generated_copies == 1
    assert stats.print_jobs == 1
    assert stats.printed_copies == 1
    assert stats.latest_output_file == output.name
    assert stats.first_print_utc


def test_mismatched_local_key_is_recovered(tmp_path):
    settings_store, history = make_history(tmp_path)
    history.secret_store.set("local-secret-that-does-not-match")
    settings = settings_store.load()
    settings["history_key_fingerprint"] = "1111111111111111"
    settings_store.save(settings)

    history.ensure_ready()
    ok, message = history.status()
    assert ok is True
    assert message == "Cronologia stampe: disponibile"
    assert history.recovered_key is True


def test_stats_survive_service_reopen(tmp_path):
    settings_store, history = make_history(tmp_path)
    settings = settings_store.load()
    code = "55555-44444"
    output = tmp_path / "Print" / "Voucher_Restart.pdf"
    batch = VoucherBatch(tmp_path / "source.pdf", [VoucherRecord(code, duration_minutes=120, recipient="Restart")], recipient="Restart")
    history.record_batch(batch, output, settings, reprint=False)
    history.record_print([code], output, 2, settings)

    # A real application restart reuses the portable key stored with data.
    reopened_store = HistoryKeyStore(history.secret_store.root)
    reopened = HistoryService(
        tmp_path / "data" / "history.jsonl",
        tmp_path / "data" / "history.lock",
        settings_store,
        secret_store=reopened_store,
    )
    stats = reopened.stats_for_codes([code], settings_store.load())[code]
    assert stats.generated_documents == 1
    assert stats.generated_copies == 1
    assert stats.print_jobs == 1
    assert stats.printed_copies == 2


def test_repeated_unlimited_labels_count_physical_copies(tmp_path):
    settings_store, history = make_history(tmp_path)
    settings = settings_store.load()
    code = "10101-20202"
    output = tmp_path / "Print" / "Voucher_Multi.pdf"
    records = [VoucherRecord(code, duration_minutes=1440, recipient="Multi") for _ in range(7)]
    batch = VoucherBatch(tmp_path / "source.pdf", records, recipient="Multi")
    history.record_batch(batch, output, settings, reprint=False)
    history.record_print(batch.codes, output, 3, settings)

    stats = history.stats_for_codes([code], settings)[code]
    assert stats.generated_documents == 1
    assert stats.generated_copies == 7
    assert stats.print_jobs == 1
    assert stats.printed_copies == 21


def test_unreadable_history_fails_closed(tmp_path):
    settings_store, history = make_history(tmp_path)
    history.history_path.mkdir(parents=True, exist_ok=True)

    try:
        history.stats_for_codes(
            ["12345-67890"],
            settings_store.load(),
        )
    except HistoryError:
        pass
    else:
        raise AssertionError("Unreadable history must not be treated as empty")


def test_corrupt_history_line_fails_closed(tmp_path):
    settings_store, history = make_history(tmp_path)
    settings = settings_store.load()
    code = "22222-33333"
    output = tmp_path / "Print" / "Voucher_Corrupt.pdf"
    batch = VoucherBatch(
        tmp_path / "source.pdf",
        [VoucherRecord(code, duration_minutes=60, recipient="Corrupt")],
        recipient="Corrupt",
    )
    history.record_batch(batch, output, settings, reprint=False)
    with history.history_path.open("a", encoding="utf-8") as handle:
        handle.write("{broken-json\n")

    try:
        history.stats_for_codes([code], settings_store.load())
    except HistoryError:
        pass
    else:
        raise AssertionError("Corrupt audit rows must fail closed")


def test_existing_history_with_missing_fingerprint_does_not_bless_key(tmp_path):
    settings_store, history = make_history(tmp_path)
    settings = settings_store.load()
    code = "44444-55555"
    output = tmp_path / "Print" / "Voucher_Existing.pdf"
    batch = VoucherBatch(
        tmp_path / "source.pdf",
        [VoucherRecord(code, duration_minutes=60, recipient="Existing")],
        recipient="Existing",
    )
    history.record_batch(batch, output, settings, reprint=False)

    settings = settings_store.load()
    settings["history_key_fingerprint"] = ""
    settings_store.save(settings)

    assert history.ensure_ready() is False
    try:
        history.stats_for_codes([code], settings_store.load())
    except HistoryError:
        pass
    else:
        raise AssertionError(
            "Existing history without a verified fingerprint must fail closed"
        )


def test_existing_history_with_mismatched_key_is_not_rotated(tmp_path):
    settings_store, history = make_history(tmp_path)
    settings = settings_store.load()
    code = "66666-77777"
    output = tmp_path / "Print" / "Voucher_Mismatch.pdf"
    batch = VoucherBatch(
        tmp_path / "source.pdf",
        [VoucherRecord(code, duration_minutes=60, recipient="Mismatch")],
        recipient="Mismatch",
    )
    history.record_batch(batch, output, settings, reprint=False)

    original_fingerprint = settings_store.load()["history_key_fingerprint"]
    history.secret_store.set("different-secret-that-must-not-be-adopted")

    assert history.ensure_ready() is False
    assert (
        settings_store.load()["history_key_fingerprint"]
        == original_fingerprint
    )


def test_new_history_never_stores_absolute_pdf_path(tmp_path):
    settings_store, history = make_history(tmp_path)
    settings = settings_store.load()
    code = "88888-99999"
    output = tmp_path / "profile" / "Print" / "Voucher_Portable.pdf"
    batch = VoucherBatch(
        tmp_path / "source.pdf",
        [VoucherRecord(code, duration_minutes=60, recipient="Portable")],
        recipient="Portable",
    )
    history.record_batch(batch, output, settings, reprint=False)
    history.record_print([code], output, 1, settings)

    raw = history.history_path.read_text(encoding="utf-8")
    assert str(output.parent) not in raw
    assert output.name in raw


def test_configured_key_cannot_replace_identity_behind_history(tmp_path):
    settings_store, history = make_history(tmp_path)
    settings = settings_store.load()
    code = "12121-34343"
    output = tmp_path / "Print" / "Voucher_Protected.pdf"
    batch = VoucherBatch(
        tmp_path / "source.pdf",
        [VoucherRecord(code, duration_minutes=60, recipient="Protected")],
        recipient="Protected",
    )
    history.record_batch(batch, output, settings, reprint=False)

    try:
        history.configure_secret(
            "different-shared-history-secret",
            settings_store.load(),
            replace=True,
        )
    except ValueError:
        pass
    else:
        raise AssertionError(
            "Existing audit rows must prevent history-key replacement"
        )


def test_codes_for_output_marks_every_linked_voucher_and_keeps_multiplicity(tmp_path):
    settings_store, history = make_history(tmp_path)
    settings = settings_store.load()
    output = tmp_path / "Print" / "Voucher_Group.pdf"
    code_a = "11111-22222"
    code_b = "33333-44444"
    batch = VoucherBatch(
        tmp_path / "source.pdf",
        [
            VoucherRecord(code_a, recipient="A"),
            VoucherRecord(code_b, recipient="B"),
            VoucherRecord(code_b, recipient="B"),
        ],
        recipient="Group",
    )
    history.record_batch(batch, output, settings, reprint=False)

    linked = history.codes_for_output(
        [code_a, code_b, "55555-66666"],
        output,
        settings,
    )

    assert linked == [code_a, code_b, code_b]
