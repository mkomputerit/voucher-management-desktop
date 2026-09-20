from pathlib import Path

from tools.export_public_snapshot import PRIVATE_ONLY_PATHS, export_snapshot


def test_public_export_excludes_private_engineering_files(tmp_path: Path):
    output = export_snapshot(tmp_path / "public")
    for relative in PRIVATE_ONLY_PATHS:
        assert not (output / relative).exists()


def test_public_export_keeps_publishable_project_files(tmp_path: Path):
    output = export_snapshot(tmp_path / "public")
    assert (output / "README.md").is_file()
    assert (
        output / "src" / "voucher_management" / "unifi_api.py"
    ).is_file()
    assert (output / "tools" / "check_public_tree.py").is_file()


def test_public_export_strips_private_workflow_steps(tmp_path: Path):
    output = export_snapshot(tmp_path / "public")
    workflow = (
        output / ".github" / "workflows" / "build-windows.yml"
    ).read_text(encoding="utf-8")
    assert "Package private engineering prerelease" not in workflow
    assert "Publish private engineering prerelease" not in workflow
    assert "contents: write" not in workflow
    assert "contents: read" in workflow
    assert "PUBLIC_PRIVACY_MARKERS" not in workflow
    assert "PRIVACY_MARKERS_FILE" not in workflow
    assert "PRIVATE_REPOSITORY_IDENTITY" not in workflow
    assert "--require-markers" not in workflow


def test_public_export_excludes_beta_engineering_records(tmp_path: Path):
    output = export_snapshot(tmp_path / "public")
    assert not (output / "docs" / "BETA_4_2_0_REVIEW.md").exists()
    assert not (output / "docs" / "BETA_4_2_0_TEST_PLAN.md").exists()


def test_private_repository_identity_guard_uses_runtime_value(tmp_path: Path, monkeypatch):
    from tools.export_public_snapshot import _assert_private_identity_absent

    output = tmp_path / "public"
    output.mkdir()
    (output / "README.md").write_text(
        "Source mirror: synthetic-owner/private-engineering-repo",
        encoding="utf-8",
    )

    try:
        _assert_private_identity_absent(
            output,
            "synthetic-owner/private-engineering-repo",
        )
    except RuntimeError as exc:
        assert "README.md" in str(exc)
    else:
        raise AssertionError("private repository identity was not detected")


def test_public_repository_environment_does_not_break_export(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.delenv("PRIVATE_REPOSITORY_IDENTITY", raising=False)
    monkeypatch.setenv(
        "GITHUB_REPOSITORY",
        "mkomputerit/voucher-management-desktop",
    )

    output = export_snapshot(tmp_path / "public")
    workflow = (
        output / ".github" / "workflows" / "build-windows.yml"
    ).read_text(encoding="utf-8")

    assert (output / "README.md").is_file()
    assert "PRIVATE_REPOSITORY_IDENTITY" not in workflow
    assert "mkomputerit/voucher-management-desktop" not in workflow


def test_public_export_excludes_internal_readiness_checklist(tmp_path: Path):
    output = export_snapshot(tmp_path / "public")

    assert not (output / "docs" / "PUBLIC_RELEASE_READINESS.md").exists()
