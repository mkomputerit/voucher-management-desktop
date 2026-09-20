from voucher_management.models import VoucherBatch, VoucherRecord
from voucher_management.pdf_render import render_batch_pdf


def test_render_creates_paginated_pdf_without_packaged_branding(tmp_path):
    vouchers = []
    for index in range(11):
        recipient = "Reception" if index == 0 else "Guests"
        vouchers.append(
            VoucherRecord(
                code=f"{10000 + index:05d}-{20000 + index:05d}",
                duration_minutes=1440,
                recipient=recipient,
            )
        )

    batch = VoucherBatch(
        source_path=tmp_path / "CONTROLLER_API",
        recipient="Reception_Guests",
        vouchers=vouchers,
    )
    output = tmp_path / "Voucher_Reception_Guests.pdf"

    render_batch_pdf(
        batch,
        output,
        {
            "preset": "Classico",
            "wifi_title": "Guest Wi-Fi",
            "structure_type": "Personalizzata",
            "structure_name": "Example Venue",
            "location": "",
            "logo_path": "",
        },
    )

    assert output.exists()
    assert output.stat().st_size > 1000
    assert output.read_bytes().startswith(b"%PDF")
