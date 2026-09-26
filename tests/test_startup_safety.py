from types import SimpleNamespace
from unittest.mock import Mock

from voucher_management import modern_app
from voucher_management.app import VoucherApp


def test_main_reports_fatal_startup_error(monkeypatch):
    def fail_startup():
        raise RuntimeError("synthetic startup failure")

    shown = []
    monkeypatch.setattr(modern_app, "ModernVoucherApp", fail_startup)
    monkeypatch.setattr(
        modern_app.messagebox,
        "showerror",
        lambda *args, **kwargs: shown.append((args, kwargs)),
    )

    assert modern_app.main() == 1
    assert shown
    assert shown[0][0][0] == "Avvio impossibile"
    assert "synthetic startup failure" not in shown[0][0][1]


def test_print_cleanup_contains_unexpected_failure(monkeypatch):
    logger = Mock()
    fake = SimpleNamespace(
        settings={"print_retention_days": 0},
        history=SimpleNamespace(
            generated_output_names=lambda: (_ for _ in ()).throw(
                OverflowError("synthetic")
            )
        ),
        paths=SimpleNamespace(prints=object()),
        logger=logger,
    )

    VoucherApp._cleanup_print_archive(fake)

    logger.warning.assert_called_once()


def test_temp_cleanup_contains_unexpected_failure(monkeypatch):
    logger = Mock()
    fake = SimpleNamespace(
        paths=SimpleNamespace(prints=object()),
        logger=logger,
    )
    monkeypatch.setattr(
        "voucher_management.app.cleanup_orphan_pdf_temps",
        lambda _path: (_ for _ in ()).throw(OverflowError("synthetic")),
    )

    VoucherApp._cleanup_orphan_pdf_temps(fake)

    logger.warning.assert_called_once()


def test_initial_sqlite_snapshot_is_rendered_after_ui_build():
    populate = Mock()
    fake = SimpleNamespace(populate=populate)

    VoucherApp._populate_initial_snapshot(fake)

    populate.assert_called_once_with()
