from __future__ import annotations

from voucher_management import pdf_render


def test_validated_logo_snapshot_reuses_image_reader_for_same_file_snapshot(
    tmp_path,
    monkeypatch,
):
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"synthetic")
    calls = []
    sentinel = object()

    monkeypatch.setattr(
        pdf_render,
        "validate_logo_image",
        lambda path: calls.append(("validate", path)),
    )
    monkeypatch.setattr(
        pdf_render,
        "ImageReader",
        lambda path: calls.append(("reader", path)) or sentinel,
    )
    pdf_render._validated_logo_snapshot.cache_clear()

    stat = logo.stat()
    first = pdf_render._validated_logo_snapshot(
        str(logo),
        stat.st_mtime_ns,
        stat.st_size,
    )
    second = pdf_render._validated_logo_snapshot(
        str(logo),
        stat.st_mtime_ns,
        stat.st_size,
    )

    assert first is sentinel
    assert second is sentinel
    assert [item[0] for item in calls] == ["validate", "reader"]


def test_validated_logo_snapshot_invalidates_when_snapshot_changes(
    tmp_path,
    monkeypatch,
):
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"synthetic")
    calls = []

    monkeypatch.setattr(
        pdf_render,
        "validate_logo_image",
        lambda path: calls.append(("validate", path)),
    )
    monkeypatch.setattr(
        pdf_render,
        "ImageReader",
        lambda path: object(),
    )
    pdf_render._validated_logo_snapshot.cache_clear()

    stat = logo.stat()
    pdf_render._validated_logo_snapshot(
        str(logo),
        stat.st_mtime_ns,
        stat.st_size,
    )
    pdf_render._validated_logo_snapshot(
        str(logo),
        stat.st_mtime_ns + 1,
        stat.st_size,
    )

    assert len(calls) == 2
