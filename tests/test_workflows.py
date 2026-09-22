from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from voucher_management.history import HistoryError, PrintStats
from voucher_management.unifi_api import (
    ApiVoucher,
    UniFiApiError,
    UniFiMutationUncertain,
)
from voucher_management.workflows import (
    ExistingPdfResolutionError,
    PrintJob,
    build_print_batch,
    create_vouchers_and_refresh,
    delete_vouchers_and_refresh,
    evaluate_delete_candidates,
    execute_print_job,
    prepare_print_job,
    refresh_delete_candidates,
    refresh_vouchers,
    resolve_existing_pdf,
    validate_create_params,
    verify_print_history_ready,
)


def voucher(
    voucher_id: str,
    code: str,
    *,
    used: int = 0,
    status: str = "VALID_MULTI",
    quota: int = 1,
    recipient: str = "Guest",
) -> ApiVoucher:
    return ApiVoucher(
        id=voucher_id,
        code=code,
        recipient=recipient,
        duration_minutes=60,
        create_time=1,
        quota=quota,
        used=used,
        status=status,
    )


class FakeClient:
    def __init__(self):
        self.created: list[ApiVoucher] = []
        self.list_result: list[ApiVoucher] = []
        self.list_error: UniFiApiError | None = None
        self.create_error: UniFiApiError | None = None
        self.current_by_id: dict[str, ApiVoucher] = {}
        self.create_calls = 0
        self.get_calls: list[str] = []
        self.delete_calls: list[list[str]] = []

    def create_vouchers(self, **params):
        self.create_calls += 1
        if self.create_error is not None:
            raise self.create_error
        return list(self.created)

    def list_vouchers(self):
        if self.list_error is not None:
            raise self.list_error
        return list(self.list_result)

    def get_voucher(self, voucher_id):
        self.get_calls.append(voucher_id)
        return self.current_by_id[voucher_id]

    def delete_vouchers(self, voucher_ids):
        self.delete_calls.append(list(voucher_ids))


def test_refresh_vouchers_is_controller_state_not_cached_state():
    client = FakeClient()
    fresh = voucher("fresh", "1111122222")
    client.list_result = [fresh]

    assert refresh_vouchers(client) == [fresh]


def test_create_success_refresh_failure_merges_created_without_recreating():
    cached = voucher("cached", "1111122222")
    created = voucher("created", "3333344444")
    client = FakeClient()
    client.created = [created]
    client.list_error = UniFiApiError("refresh unavailable")

    outcome = create_vouchers_and_refresh(
        client,
        [cached],
        {"recipient": "Guest", "quantity": 1},
    )

    assert client.create_calls == 1
    assert outcome.created == (created,)
    assert {item.id for item in outcome.vouchers} == {"cached", "created"}
    assert str(outcome.refresh_error) == "refresh unavailable"


def test_uncertain_create_never_replays_post_and_refreshes_controller_state():
    cached = voucher("cached", "1111122222")
    controller_new = voucher("controller-new", "3333344444")
    client = FakeClient()
    client.create_error = UniFiMutationUncertain("la creazione dei voucher")
    client.list_result = [cached, controller_new]

    outcome = create_vouchers_and_refresh(
        client,
        [cached],
        {"recipient": "Guest", "quantity": 1},
    )

    assert client.create_calls == 1
    assert outcome.created == ()
    assert outcome.vouchers == (cached, controller_new)
    assert isinstance(outcome.uncertain_error, UniFiMutationUncertain)
    assert outcome.refresh_error is None


def test_uncertain_create_refresh_failure_keeps_cache_without_replaying_post():
    cached = voucher("cached", "1111122222")
    client = FakeClient()
    client.create_error = UniFiMutationUncertain("la creazione dei voucher")
    client.list_error = UniFiApiError("refresh unavailable")

    outcome = create_vouchers_and_refresh(
        client,
        [cached],
        {"recipient": "Guest", "quantity": 1},
    )

    assert client.create_calls == 1
    assert outcome.created == ()
    assert outcome.vouchers == (cached,)
    assert isinstance(outcome.uncertain_error, UniFiMutationUncertain)
    assert str(outcome.refresh_error) == "refresh unavailable"


def test_create_success_prefers_fresh_controller_list():
    created = voucher("created", "3333344444")
    server_copy = voucher(
        "created",
        "3333344444",
        recipient="Normalized by controller",
    )
    other = voucher("other", "5555566666")
    client = FakeClient()
    client.created = [created]
    client.list_result = [server_copy, other]

    outcome = create_vouchers_and_refresh(
        client,
        [],
        {"recipient": "Guest", "quantity": 1},
    )

    assert outcome.refresh_error is None
    assert outcome.vouchers == (server_copy, other)


def test_delete_candidates_are_reread_before_policy():
    cached = voucher("v1", "1111122222", used=0)
    now_used = voucher(
        "v1",
        "1111122222",
        used=1,
        status="USED_MULTIPLE",
    )
    client = FakeClient()
    client.current_by_id = {"v1": now_used}

    current = refresh_delete_candidates(client, [cached])
    blocked = evaluate_delete_candidates(
        current,
        {now_used.code_formatted: PrintStats()},
    )

    assert client.get_calls == ["v1"]
    assert current == [now_used]
    assert len(blocked) == 1
    assert blocked[0].voucher is now_used
    assert blocked[0].policy.reason == "in_use"
    assert client.delete_calls == []


def test_delete_policy_also_blocks_locally_printed_voucher():
    current = voucher("v1", "1111122222")
    blocked = evaluate_delete_candidates(
        [current],
        {
            current.code_formatted: PrintStats(
                print_jobs=1,
                printed_copies=1,
            )
        },
    )

    assert len(blocked) == 1
    assert blocked[0].policy.reason == "printed"


def test_delete_success_refresh_failure_removes_deleted_rows_from_cache():
    deleted = voucher("delete-me", "1111122222")
    survivor = voucher("keep-me", "3333344444")
    client = FakeClient()
    client.list_error = UniFiApiError("refresh unavailable")

    outcome = delete_vouchers_and_refresh(
        client,
        [deleted, survivor],
        [deleted],
    )

    assert client.delete_calls == [["delete-me"]]
    assert outcome.vouchers == (survivor,)
    assert str(outcome.refresh_error) == "refresh unavailable"


def test_delete_failure_is_not_converted_to_refresh_success():
    current = voucher("v1", "1111122222")

    class DeleteFailClient(FakeClient):
        def delete_vouchers(self, voucher_ids):
            raise UniFiApiError("delete failed")

    client = DeleteFailClient()
    client.list_result = []

    with pytest.raises(UniFiApiError, match="delete failed"):
        delete_vouchers_and_refresh(client, [current], [current])


def test_build_print_batch_repeats_one_unlimited_voucher():
    unlimited = voucher(
        "v1",
        "1111122222",
        quota=0,
        recipient="Reception",
    )

    batch = build_print_batch([unlimited], unlimited_copies=3)

    assert batch.recipient == "Reception"
    assert batch.codes == ["11111-22222"] * 3
    assert [item.recipient for item in batch.vouchers] == ["Reception"] * 3


def test_build_print_batch_does_not_repeat_normal_multi_selection():
    first = voucher("v1", "1111122222", quota=0, recipient="")
    second = voucher("v2", "3333344444", quota=0, recipient="Guest B")

    batch = build_print_batch(
        [first, second],
        unlimited_copies=8,
    )

    assert batch.codes == ["11111-22222", "33333-44444"]
    assert [item.recipient for item in batch.vouchers] == ["Guest", "Guest B"]


@pytest.mark.parametrize("copies", (0, 1000))
def test_build_print_batch_rejects_invalid_unlimited_copy_count(copies):
    current = voucher("v1", "1111122222", quota=0)

    with pytest.raises(ValueError, match="Numero copie"):
        build_print_batch([current], unlimited_copies=copies)


@pytest.mark.parametrize(
    ("mode", "quota", "expected_quota"),
    (
        ("Monouso", "999", 1),
        ("Multiuso", "7", 7),
        ("Multiuso illimitato", "999", 0),
    ),
)
def test_validate_create_params_normalizes_usage_modes(
    mode,
    quota,
    expected_quota,
):
    params = validate_create_params(
        recipient="  Reception  ",
        quantity="2",
        mode=mode,
        quota=quota,
        expire_number="24",
        expire_unit="Ore",
        data_mb="1024",
        down_mbps="50",
        up_mbps="25",
    )

    assert params == {
        "recipient": "Reception",
        "quantity": 2,
        "expire_number": 24,
        "expire_unit": 60,
        "quota": expected_quota,
        "data_mb": 1024,
        "down_mbps": 50,
        "up_mbps": 25,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("recipient", ""),
        ("quantity", "0"),
        ("quantity", "51"),
        ("expire_number", "0"),
        ("mode", "Sconosciuto"),
        ("quota", "1"),
        ("expire_unit", "Settimane"),
        ("data_mb", "0"),
        ("data_mb", "1048577"),
        ("down_mbps", "101"),
        ("up_mbps", "-1"),
    ),
)
def test_validate_create_params_rejects_invalid_limits(field, value):
    values = {
        "recipient": "Guest",
        "quantity": "1",
        "mode": "Multiuso",
        "quota": "2",
        "expire_number": "24",
        "expire_unit": "Ore",
        "data_mb": "",
        "down_mbps": "",
        "up_mbps": "",
    }
    values[field] = value

    with pytest.raises(ValueError):
        validate_create_params(**values)


def test_validate_create_params_keeps_optional_limits_unlimited():
    params = validate_create_params(
        recipient="Guest",
        quantity=1,
        mode="Monouso",
        quota=2,
        expire_number=30,
        expire_unit="Minuti",
        data_mb="",
        down_mbps=" ",
        up_mbps=None,
    )

    assert params["data_mb"] is None
    assert params["down_mbps"] is None
    assert params["up_mbps"] is None
    assert params["expire_unit"] == 1


def test_prepare_print_job_resolves_normal_batch_path(tmp_path):
    current = voucher(
        "v1",
        "1111122222",
        recipient="Reception",
    )
    now = datetime(2026, 9, 21, 19, 30)

    job = prepare_print_job(
        [current],
        tmp_path,
        unlimited_copies=1,
        now=now,
    )

    assert job.batch.codes == ["11111-22222"]
    assert job.batch.recipient == "Reception"
    assert job.output.parent.name == "09"
    assert job.output.parent.parent.name == "2026"
    assert job.output.name.startswith("Voucher_")


def test_prepare_print_job_repeats_only_single_unlimited_voucher(tmp_path):
    current = voucher(
        "v1",
        "1111122222",
        quota=0,
        recipient="Reception",
    )

    job = prepare_print_job(
        [current],
        tmp_path,
        unlimited_copies=4,
        now=datetime(2026, 9, 21, 19, 30),
    )

    assert job.batch.codes == ["11111-22222"] * 4


def test_execute_print_job_marks_reprint_and_records_after_render(tmp_path):
    calls = []
    job = PrintJob(
        batch=build_print_batch(
            [voucher("v1", "1111122222")],
        ),
        output=tmp_path / "Voucher_Test.pdf",
    )

    class History:
        def find_duplicates(self, codes, settings):
            calls.append(("duplicates", list(codes)))
            return ["existing"]

        def record_batch(self, batch, output, settings, *, reprint):
            calls.append(("record", Path(output), reprint))

    outcome = execute_print_job(
        job,
        history=History(),
        settings={"structure_name": "Test"},
        render_pdf=lambda batch, output, settings: calls.append(
            ("render", Path(output))
        ),
    )

    assert [item[0] for item in calls] == [
        "render",
        "duplicates",
        "record",
    ]
    assert calls[-1][2] is True
    assert outcome.reprint is True
    assert outcome.codes == ("11111-22222",)


def test_execute_print_job_propagates_history_failure_without_hiding_it(tmp_path):
    job = PrintJob(
        batch=build_print_batch(
            [voucher("v1", "1111122222")],
        ),
        output=tmp_path / "Voucher_Test.pdf",
    )

    class History:
        def find_duplicates(self, codes, settings):
            return []

        def record_batch(self, *args, **kwargs):
            raise HistoryError("history unavailable")

    with pytest.raises(HistoryError, match="history unavailable"):
        execute_print_job(
            job,
            history=History(),
            settings={},
            render_pdf=lambda *args: None,
        )


def test_verify_print_history_ready_delegates_to_history():
    current = voucher("v1", "1111122222")

    class History:
        def stats_for_codes(self, codes, settings):
            raise HistoryError("identity mismatch")

    with pytest.raises(HistoryError, match="identity mismatch"):
        verify_print_history_ready(
            [current],
            history=History(),
            settings={},
        )


class ExistingPdfHistory:
    def __init__(self, latest_output_file, linked_codes):
        self.latest_output_file = latest_output_file
        self.linked_codes = list(linked_codes)
        self.paths_seen = []

    def stats_for_codes(self, codes, settings):
        return {
            codes[0]: PrintStats(
                generated_documents=1,
                latest_output_file=str(self.latest_output_file),
            )
        }

    def codes_for_output(self, codes, path, settings):
        self.paths_seen.append(Path(path))
        return [
            code
            for code in self.linked_codes
            if code in codes
        ]


def test_resolve_existing_pdf_uses_portable_filename_and_all_linked_codes(
    tmp_path,
):
    archive = tmp_path / "Print"
    moved = archive / "2026" / "09" / "Voucher_Group.pdf"
    moved.parent.mkdir(parents=True)
    moved.write_bytes(b"%PDF-test")

    first = voucher("v1", "1111122222")
    second = voucher("v2", "3333344444")
    history = ExistingPdfHistory(
        "Voucher_Group.pdf",
        [first.code_formatted, second.code_formatted],
    )

    resolved = resolve_existing_pdf(
        first,
        [first, second],
        history=history,
        settings={},
        prints_root=archive,
        finder=lambda root, name: [moved],
    )

    assert resolved.path == moved
    assert resolved.linked_codes == (
        "11111-22222",
        "33333-44444",
    )


def test_resolve_existing_pdf_accepts_existing_legacy_absolute_path(tmp_path):
    legacy = (tmp_path / "old" / "Voucher_Legacy.pdf").resolve()
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"%PDF-test")
    current = voucher("v1", "1111122222")
    history = ExistingPdfHistory(
        legacy,
        [current.code_formatted],
    )

    resolved = resolve_existing_pdf(
        current,
        [current],
        history=history,
        settings={},
        prints_root=tmp_path / "Print",
        finder=lambda root, name: pytest.fail(
            "existing absolute path should not need archive search"
        ),
    )

    assert resolved.path == legacy


def test_resolve_existing_pdf_recovers_moved_legacy_file(tmp_path):
    old = (tmp_path / "old" / "Voucher_Moved.pdf").resolve()
    moved = tmp_path / "Print" / "2026" / "09" / old.name
    moved.parent.mkdir(parents=True)
    moved.write_bytes(b"%PDF-test")
    current = voucher("v1", "1111122222")
    history = ExistingPdfHistory(
        old,
        [current.code_formatted],
    )

    resolved = resolve_existing_pdf(
        current,
        [current],
        history=history,
        settings={},
        prints_root=tmp_path / "Print",
        finder=lambda root, name: [moved],
    )

    assert resolved.path == moved


def test_resolve_existing_pdf_reports_missing_file(tmp_path):
    current = voucher("v1", "1111122222")
    history = ExistingPdfHistory(
        "Voucher_Missing.pdf",
        [current.code_formatted],
    )

    with pytest.raises(ExistingPdfResolutionError) as captured:
        resolve_existing_pdf(
            current,
            [current],
            history=history,
            settings={},
            prints_root=tmp_path / "Print",
            finder=lambda root, name: [],
        )

    assert captured.value.reason == "missing_file"
    assert captured.value.path == tmp_path / "Print" / "Voucher_Missing.pdf"


def test_resolve_existing_pdf_reports_unverifiable_linkage(tmp_path):
    path = tmp_path / "Print" / "Voucher_Link.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"%PDF-test")
    current = voucher("v1", "1111122222")
    other = voucher("v2", "3333344444")
    history = ExistingPdfHistory(
        path,
        [other.code_formatted],
    )

    with pytest.raises(ExistingPdfResolutionError) as captured:
        resolve_existing_pdf(
            current,
            [current, other],
            history=history,
            settings={},
            prints_root=tmp_path / "Print",
        )

    assert captured.value.reason == "linkage_mismatch"


def test_resolve_existing_pdf_reports_no_recorded_document(tmp_path):
    current = voucher("v1", "1111122222")

    class History:
        def stats_for_codes(self, codes, settings):
            return {codes[0]: PrintStats()}

    with pytest.raises(ExistingPdfResolutionError) as captured:
        resolve_existing_pdf(
            current,
            [current],
            history=History(),
            settings={},
            prints_root=tmp_path / "Print",
        )

    assert captured.value.reason == "not_recorded"
