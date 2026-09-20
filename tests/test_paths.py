import json

from voucher_management import paths as paths_module
from voucher_management.paths import AppPaths


def test_legacy_user_data_is_migrated_without_overwrite(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    legacy = tmp_path / "UniFiVoucherTool"
    (legacy / "config").mkdir(parents=True)
    (legacy / "data").mkdir(parents=True)
    (legacy / "Print" / "2026").mkdir(parents=True)
    (legacy / "Loghi").mkdir(parents=True)

    (legacy / "config" / "settings.json").write_text(
        json.dumps({"wifi_title": "Legacy Wi-Fi"}),
        encoding="utf-8",
    )
    (legacy / "data" / "history.jsonl").write_text(
        '{"event":"generate"}\n',
        encoding="utf-8",
    )
    (legacy / "data" / "history_secret.key").write_bytes(b"legacy-key")
    (legacy / "Print" / "2026" / "voucher.pdf").write_bytes(b"PDF")
    (legacy / "Loghi" / "logo.png").write_bytes(b"PNG")

    paths = AppPaths()
    paths.ensure_writable()

    assert paths.user_root == tmp_path / "VoucherManagement"
    assert paths.settings.exists()
    assert paths.history.exists()
    assert (paths.data / "history_secret.key").read_bytes() == b"legacy-key"
    assert (paths.prints / "2026" / "voucher.pdf").exists()
    assert (paths.logos / "logo.png").exists()


def test_migration_never_overwrites_newer_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    legacy = tmp_path / "UniFiVoucherTool" / "config"
    current = tmp_path / "VoucherManagement" / "config"
    legacy.mkdir(parents=True)
    current.mkdir(parents=True)

    (legacy / "settings.json").write_text(
        '{"wifi_title":"legacy"}',
        encoding="utf-8",
    )
    (current / "settings.json").write_text(
        '{"wifi_title":"current"}',
        encoding="utf-8",
    )

    paths = AppPaths()
    paths.ensure_writable()

    assert '"current"' in paths.settings.read_text(encoding="utf-8")


def test_external_configured_logo_is_imported_into_persistent_library(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "profile"))
    external = tmp_path / "external" / "venue.png"
    external.parent.mkdir(parents=True)
    external.write_bytes(b"PNG")

    paths = AppPaths()
    paths.ensure_writable()
    migrated = paths.persist_configured_logo(str(external))

    assert migrated == str(paths.logos / "venue.png")
    assert (paths.logos / "venue.png").read_bytes() == b"PNG"


def test_already_migrated_logo_is_reused_when_original_is_missing(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "profile"))
    paths = AppPaths()
    paths.ensure_writable()
    (paths.logos / "legacy.png").write_bytes(b"PNG")

    migrated = paths.persist_configured_logo(
        str(tmp_path / "missing-share" / "legacy.png")
    )

    assert migrated == str(paths.logos / "legacy.png")


def test_unavailable_legacy_logo_does_not_break_startup_migration(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "profile"))
    external = tmp_path / "external" / "venue.png"
    external.parent.mkdir(parents=True)
    external.write_bytes(b"PNG")

    paths = AppPaths()
    paths.ensure_writable()

    def fail_copy(_source, _target):
        raise OSError("simulated unavailable share")

    monkeypatch.setattr(paths_module.shutil, "copy2", fail_copy)

    migrated = paths.persist_configured_logo(str(external))

    assert migrated == str(external)
