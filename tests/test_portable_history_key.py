from pathlib import Path

from voucher_management.security.history_key import HistoryKeyError, HistoryKeyStore


def test_portable_history_key_roundtrip(tmp_path: Path):
    """History identity must move with application data across profiles."""
    source_root = tmp_path / "profile-a"
    restored_root = tmp_path / "profile-b"

    source = HistoryKeyStore(source_root)
    history_identity = "0123456789abcdef" * 2
    source.set(history_identity)

    restored = HistoryKeyStore(restored_root)
    restored.path.parent.mkdir(parents=True, exist_ok=True)
    restored.path.write_bytes(source.path.read_bytes())

    assert restored.get() == history_identity
    assert restored.path.read_bytes().startswith(
        HistoryKeyStore.PORTABLE_PREFIX
    )


def test_legacy_portable_prefix_is_upgraded_in_place(tmp_path: Path):
    """Rebranding must not break beta history correlation."""
    store = HistoryKeyStore(tmp_path / "profile")
    history_identity = "abcdef0123456789" * 2
    import base64

    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(
        HistoryKeyStore.LEGACY_PORTABLE_PREFIX
        + base64.b64encode(history_identity.encode("utf-8"))
    )

    assert store.get() == history_identity
    assert store.path.read_bytes().startswith(
        HistoryKeyStore.PORTABLE_PREFIX
    )


def test_legacy_dpapi_path_can_be_supplied_for_migration(tmp_path: Path):
    """The store keeps explicit legacy roots separate from current storage."""
    current = tmp_path / "current"
    legacy = tmp_path / "legacy"
    store = HistoryKeyStore(current, legacy_roots=(legacy,))

    assert store.path == current / "data" / "history_secret.key"
    assert legacy / "history_secret.bin" in store.legacy_paths


def test_too_short_history_identity_is_rejected(tmp_path: Path):
    store = HistoryKeyStore(tmp_path / "profile")

    try:
        store.set("too-short")
    except HistoryKeyError:
        pass
    else:
        raise AssertionError("Short history identities must be rejected")
