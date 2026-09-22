import json

import pytest

from voucher_management.settings import DEFAULT_SETTINGS, SettingsStore


def test_public_defaults_contain_no_operational_controller_address(tmp_path):
    assert DEFAULT_SETTINGS["controller_api_root"] == ""
    assert DEFAULT_SETTINGS["controller_cert_sha256"] == ""
    assert DEFAULT_SETTINGS["structure_name"] == ""
    assert DEFAULT_SETTINGS["wifi_title"] == "Guest Wi-Fi"
    assert DEFAULT_SETTINGS["print_retention_days"] == 0

    store = SettingsStore(tmp_path / "config" / "settings.json")
    assert store.load()["controller_api_root"] == ""


def test_4_2_settings_without_retention_keep_pdfs_by_default(tmp_path):
    path = tmp_path / "config" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"wifi_title": "Legacy 4.2"}),
        encoding="utf-8",
    )

    loaded = SettingsStore(path).load()

    assert loaded["print_retention_days"] == 0


def test_malformed_settings_fall_back_to_safe_defaults(tmp_path):
    path = tmp_path / "config" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    store = SettingsStore(path)
    loaded = store.load()

    assert loaded["controller_api_root"] == ""
    assert loaded["controller_cert_sha256"] == ""
    assert store.consume_warning()
    preserved = list(path.parent.glob("settings.json.corrupt-*"))
    assert len(preserved) == 1
    assert preserved[0].read_text(encoding="utf-8") == "{broken"


def test_legacy_controller_host_is_imported_but_old_username_is_not(tmp_path):
    path = tmp_path / "config" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "cloud_key_host": "controller.example.invalid",
                "remember_username": True,
                "saved_username": "legacy-user",
            }
        ),
        encoding="utf-8",
    )

    store = SettingsStore(path)
    loaded = store.load()

    assert loaded["controller_api_root"] == "controller.example.invalid"
    assert "remember_username" not in loaded
    assert "saved_username" not in loaded

    store.save(loaded)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["controller_api_root"] == "controller.example.invalid"
    assert "cloud_key_host" not in raw
    assert "remember_username" not in raw
    assert "saved_username" not in raw


def test_save_preserves_known_defaults_and_user_values(tmp_path):
    path = tmp_path / "config" / "settings.json"
    store = SettingsStore(path)
    settings = store.load()
    settings["wifi_title"] = "Visitors"
    settings["controller_cert_sha256"] = "a" * 64
    store.save(settings)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["wifi_title"] == "Visitors"
    assert raw["controller_cert_sha256"] == "a" * 64
    assert "history_key_fingerprint" in raw


def test_unknown_credential_like_keys_are_not_loaded_or_resaved(tmp_path):
    path = tmp_path / "config" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "wifi_title": "Visitors",
                "password": "must-not-survive",
                "api_key": "must-not-survive",
            }
        ),
        encoding="utf-8",
    )

    store = SettingsStore(path)
    loaded = store.load()
    assert "password" not in loaded
    assert "api_key" not in loaded

    store.save(loaded)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "password" not in raw
    assert "api_key" not in raw


def test_legacy_unverified_tls_flag_is_dropped(tmp_path):
    path = tmp_path / "config" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"allow_unverified_tls": True}),
        encoding="utf-8",
    )

    store = SettingsStore(path)
    loaded = store.load()
    assert "allow_unverified_tls" not in loaded
    assert loaded["controller_cert_sha256"] == ""

    store.save(loaded)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "allow_unverified_tls" not in raw


def test_update_preserves_safety_fields_written_after_stale_ui_load(tmp_path):
    path = tmp_path / "config" / "settings.json"
    store = SettingsStore(path)

    stale_ui_copy = store.load()
    assert stale_ui_copy["history_key_fingerprint"] == ""

    # Simulate HistoryService initializing the HMAC identity after the UI copy
    # was loaded.
    latest = store.load()
    latest["history_key_fingerprint"] = "0123456789abcdef"
    store.save(latest)

    merged = store.update(controller_api_root="https://controller.invalid/v1")

    assert merged["history_key_fingerprint"] == "0123456789abcdef"
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["history_key_fingerprint"] == "0123456789abcdef"
    assert persisted["controller_api_root"] == "https://controller.invalid/v1"


def test_update_rejects_unknown_keys(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")

    credential_key = "api_" + "key"
    credential_value = "must-" + "never-persist"
    try:
        store.update(**{credential_key: credential_value})
    except KeyError:
        pass
    else:
        raise AssertionError("unknown/credential settings key was accepted")

def test_malformed_settings_preserve_only_one_copy_per_content(tmp_path):
    path = tmp_path / "config" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    store = SettingsStore(path)

    for _ in range(6):
        store.load()

    preserved = list(path.parent.glob("settings.json.corrupt-*"))
    assert len(preserved) == 1
    assert preserved[0].read_text(encoding="utf-8") == "{broken"

    path.write_text("{different-broken", encoding="utf-8")
    store.load()

    preserved = list(path.parent.glob("settings.json.corrupt-*"))
    assert len(preserved) == 2




@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("print_retention_days", 10**6, 0),
        ("print_retention_days", 10**9, 0),
        ("print_retention_days", 10**12, 0),
        ("print_retention_days", True, 0),
        ("log_retention_days", "abc", 30),
        ("log_retention_days", False, 30),
        ("log_retention_days", 0, 30),
        ("log_retention_days", 3651, 30),
    ],
)
def test_invalid_numeric_settings_fall_back_to_safe_defaults(
    tmp_path,
    key,
    value,
    expected,
):
    path = tmp_path / "config" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({key: value}), encoding="utf-8")

    store = SettingsStore(path)
    loaded = store.load()

    assert loaded[key] == expected
    assert key in store.consume_warning()


def test_numeric_settings_accept_only_real_ints_in_range(tmp_path):
    path = tmp_path / "config" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "print_retention_days": 3650,
                "log_retention_days": 1,
            }
        ),
        encoding="utf-8",
    )

    loaded = SettingsStore(path).load()

    assert loaded["print_retention_days"] == 3650
    assert loaded["log_retention_days"] == 1


def test_update_sanitizes_invalid_numeric_values(tmp_path):
    store = SettingsStore(tmp_path / "config" / "settings.json")

    updated = store.update(
        print_retention_days=True,
        log_retention_days="abc",
    )

    assert updated["print_retention_days"] == 0
    assert updated["log_retention_days"] == 30
    persisted = json.loads(store.path.read_text(encoding="utf-8"))
    assert persisted["print_retention_days"] == 0
    assert persisted["log_retention_days"] == 30
