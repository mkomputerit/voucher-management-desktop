import json

from PIL import Image

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
    Image.new("RGB", (8, 8), "white").save(legacy / "Loghi" / "logo.png", format="PNG")

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
    Image.new("RGB", (8, 8), "white").save(external, format="PNG")

    paths = AppPaths()
    paths.ensure_writable()
    migrated = paths.persist_configured_logo(str(external))

    assert migrated == str(paths.logos / "venue.png")
    assert (paths.logos / "venue.png").is_file()


def test_already_migrated_logo_is_reused_when_original_is_missing(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "profile"))
    paths = AppPaths()
    paths.ensure_writable()
    Image.new("RGB", (8, 8), "white").save(paths.logos / "legacy.png", format="PNG")

    migrated = paths.persist_configured_logo(
        str(tmp_path / "missing-share" / "legacy.png")
    )

    assert migrated == str(paths.logos / "legacy.png")


def test_large_4_2_0_logo_is_preserved_during_startup_migration(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "profile"))
    external = tmp_path / "external" / "legacy-large.png"
    external.parent.mkdir(parents=True)
    Image.new("1", (6000, 4000), 1).save(external, format="PNG")

    paths = AppPaths()
    paths.ensure_writable()

    migrated = paths.persist_configured_logo(str(external))

    assert migrated == str(paths.logos / "legacy-large.png")
    assert (paths.logos / "legacy-large.png").is_file()


def test_disguised_legacy_logo_is_not_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "profile"))
    external = tmp_path / "external" / "venue.png"
    external.parent.mkdir(parents=True)
    Image.new("RGB", (8, 8), "white").save(external, format="GIF")

    paths = AppPaths()
    paths.ensure_writable()

    migrated = paths.persist_configured_logo(str(external))

    assert migrated == ""
    assert not (paths.logos / "venue.png").exists()


def test_invalid_configured_logo_exposes_one_time_warning(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "profile"))
    external = tmp_path / "external" / "venue.png"
    external.parent.mkdir(parents=True)
    Image.new("RGB", (8, 8), "white").save(
        external,
        format="GIF",
    )

    paths = AppPaths()
    paths.ensure_writable()

    assert paths.persist_configured_logo(str(external)) == ""
    warning = paths.consume_logo_warning()
    assert "non è più valido" in warning
    assert paths.consume_logo_warning() == ""


def test_unavailable_legacy_logo_does_not_break_startup_migration(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "profile"))
    external = tmp_path / "external" / "venue.png"
    external.parent.mkdir(parents=True)
    Image.new("RGB", (8, 8), "white").save(external, format="PNG")

    paths = AppPaths()
    paths.ensure_writable()

    def fail_copy(_source, _target):
        raise OSError("simulated unavailable share")

    monkeypatch.setattr(paths_module.shutil, "copy2", fail_copy)

    migrated = paths.persist_configured_logo(str(external))

    assert migrated == str(external)


def test_installed_marker_uses_shared_programdata_without_implicit_profile_import(
    tmp_path,
    monkeypatch,
):
    program = tmp_path / "ProgramFiles" / "VoucherManagement"
    program.mkdir(parents=True)
    (program / "voucher-management-deployment.json").write_text(
        '{"format":1,"mode":"shared_programdata"}',
        encoding="utf-8",
    )
    profile = tmp_path / "profile"
    programdata = tmp_path / "ProgramData"
    monkeypatch.setenv("LOCALAPPDATA", str(profile))
    monkeypatch.setenv("PROGRAMDATA", str(programdata))

    legacy = profile / "VoucherManagement" / "config"
    legacy.mkdir(parents=True)
    (legacy / "settings.json").write_text(
        '{"wifi_title":"per-user"}',
        encoding="utf-8",
    )

    paths = AppPaths(base_override=program)
    paths.ensure_writable()

    assert paths.shared_mode is True
    assert paths.user_root == programdata / "VoucherManagement"
    assert paths.per_user_root == profile / "VoucherManagement"
    assert paths.database == (
        programdata / "VoucherManagement" / "data" / "voucher_management.db"
    )
    assert not paths.settings.exists()
    assert paths.per_user_root in paths.legacy_user_roots


def test_portable_mode_remains_per_user_even_when_programdata_exists(
    tmp_path,
    monkeypatch,
):
    program = tmp_path / "portable"
    program.mkdir()
    profile = tmp_path / "profile"
    monkeypatch.setenv("LOCALAPPDATA", str(profile))
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))

    paths = AppPaths(base_override=program)

    assert paths.shared_mode is False
    assert paths.user_root == profile / "VoucherManagement"


def test_invalid_deployment_marker_fails_closed(tmp_path):
    program = tmp_path / "program"
    program.mkdir()
    (program / "voucher-management-deployment.json").write_text(
        '{"format":99,"mode":"shared_programdata"}',
        encoding="utf-8",
    )

    try:
        AppPaths(base_override=program)
    except RuntimeError as exc:
        assert "Marker di installazione" in str(exc)
    else:
        raise AssertionError("invalid deployment marker must fail closed")
