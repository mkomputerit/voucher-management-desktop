from pathlib import Path

from tools.check_public_tree import GENERIC_PRIVATE_PATTERNS, load_markers, main, scan


def test_external_marker_blocks_text_without_echoing_plaintext(tmp_path: Path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "README.md").write_text(
        "Synthetic Venue Alpha",
        encoding="utf-8",
    )
    marker_file = tmp_path / "markers.txt"
    marker_file.write_text(
        "# deployment values\nvenue alpha\n",
        encoding="utf-8",
    )

    markers = load_markers(marker_file)
    violations = scan(root, markers=markers)

    assert violations == [
        "external private marker #1 matched in text content"
    ]
    assert "venue alpha" not in " ".join(violations).casefold()


def test_external_marker_blocks_asset_path_case_insensitively(tmp_path: Path):
    root = tmp_path / "tree"
    asset = root / "assets" / "default"
    asset.mkdir(parents=True)
    (asset / "SyntheticVenue-header.png").write_bytes(b"PNG")
    marker_file = tmp_path / "markers.txt"
    marker_file.write_text("syntheticvenue\n", encoding="utf-8")

    violations = scan(root, markers=load_markers(marker_file))

    assert violations == [
        "external private marker #1 matched in a path"
    ]


def test_empty_or_comment_only_marker_file_adds_no_marker_rules(tmp_path: Path):
    marker_file = tmp_path / "markers.txt"
    marker_file.write_text("# no deployment markers\n\n", encoding="utf-8")

    assert load_markers(marker_file) == ()


def test_generic_private_ipv4_pattern_detects_supported_ranges():
    pattern = GENERIC_PRIVATE_PATTERNS["private IPv4 address"]
    values = (
        "10" + ".44.1.2",
        "192" + ".168.1.10",
        "172" + ".16.5.9",
        "172" + ".31.255.254",
    )
    for value in values:
        assert pattern.search(value), value


def test_generic_credential_pattern_detects_password_and_api_key():
    pattern = GENERIC_PRIVATE_PATTERNS["hard-coded credential value"]
    password_sample = "password" + ' = "' + "hunter2secret" + '"'
    api_key_sample = "api_" + 'key: "' + "abcd1234" + '"'
    assert pattern.search(password_sample)
    assert pattern.search(api_key_sample)


def test_generic_windows_user_path_accepts_backslash_and_slash():
    pattern = GENERIC_PRIVATE_PATTERNS["Windows user-profile path"]
    backslash_path = "C:" + "\\" + "Users" + "\\" + "operator" + "\\" + "Desktop"
    slash_path = "C:" + "/" + "Users" + "/" + "operator" + "/" + "Desktop"
    assert pattern.search(backslash_path)
    assert pattern.search(slash_path)


def test_scan_reports_each_generic_pattern(tmp_path: Path):
    root = tmp_path / "tree"
    root.mkdir()
    ip_sample = "10" + ".44.1.2"
    credential_sample = "password" + ' = "' + "hunter2secret" + '"'
    path_sample = "C:" + "/" + "Users" + "/" + "operator" + "/" + "Desktop"
    (root / "sample.md").write_text(
        "\n".join((ip_sample, credential_sample, path_sample)) + "\n",
        encoding="utf-8",
    )

    violations = scan(root)

    assert any(item.startswith("private IPv4 address:") for item in violations)
    assert any(item.startswith("hard-coded credential value:") for item in violations)
    assert any(item.startswith("Windows user-profile path:") for item in violations)


def test_require_markers_rejects_empty_file(tmp_path: Path):
    root = tmp_path / "tree"
    root.mkdir()
    marker_file = tmp_path / "markers.txt"
    marker_file.write_text("# comments only\n\n", encoding="utf-8")

    try:
        main([
            "--root", str(root),
            "--markers-file", str(marker_file),
            "--require-markers",
        ])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("--require-markers accepted an empty marker file")
