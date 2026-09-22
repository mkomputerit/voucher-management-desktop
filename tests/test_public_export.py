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
    assert (output / "assets" / "fonts" / "NotoSans-Regular.ttf").is_file()
    assert (output / "assets" / "fonts" / "NotoSans-Bold.ttf").is_file()
    assert (output / "assets" / "fonts" / "NotoSans-Italic.ttf").is_file()
    assert (
        output / "third_party_licenses" / "noto-sans" / "OFL.txt"
    ).is_file()


def test_public_export_strips_private_workflow_steps(tmp_path: Path):
    output = export_snapshot(tmp_path / "public")
    workflow = (
        output / ".github" / "workflows" / "build-windows.yml"
    ).read_text(encoding="utf-8")
    assert "Package private engineering prerelease" not in workflow
    assert "Publish private engineering prerelease" not in workflow
    assert "contents: read" in workflow
    assert workflow.count("contents: write") == 1
    test_job, release_job = workflow.split("\n  release:\n", 1)
    assert "contents: write" not in test_job
    assert "contents: write" in release_job
    assert "PUBLIC_PRIVACY_MARKERS" in workflow
    assert "PRIVACY_MARKERS_FILE" in workflow
    assert "--require-markers" in workflow
    assert "Prepare privacy markers" in workflow
    assert "Check deployment markers in source tree" in workflow
    assert "Check deployment markers in public snapshot" in workflow
    assert "Prepare private privacy markers" not in workflow
    assert "voucher-management-private-markers.txt" not in workflow
    assert "PRIVATE_REPOSITORY_IDENTITY" not in workflow




def test_public_export_requires_runtime_privacy_markers(tmp_path: Path):
    output = export_snapshot(tmp_path / "public")
    workflow = (
        output / ".github" / "workflows" / "build-windows.yml"
    ).read_text(encoding="utf-8")

    assert "PUBLIC_PRIVACY_MARKERS: ${{ secrets.PUBLIC_PRIVACY_MARKERS }}" in workflow
    assert workflow.count("--require-markers") == 2
    assert "voucher-management-privacy-markers.txt" in workflow


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

def test_exporting_an_already_public_snapshot_is_idempotent(
    tmp_path: Path,
    monkeypatch,
):
    import tools.export_public_snapshot as exporter

    def snapshot_bytes(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    first = export_snapshot(tmp_path / "first-public")
    monkeypatch.setattr(exporter, "ROOT", first)

    second = exporter.export_snapshot(tmp_path / "second-public")
    monkeypatch.setattr(exporter, "ROOT", second)

    third = exporter.export_snapshot(tmp_path / "third-public")

    assert snapshot_bytes(second) == snapshot_bytes(first)
    assert snapshot_bytes(third) == snapshot_bytes(first)

    workflow = (
        third / ".github" / "workflows" / "build-windows.yml"
    ).read_text(encoding="utf-8")
    assert workflow.count(
        "startsWith(github.ref, 'refs/heads/release/')"
    ) == 1

    for relative in PRIVATE_ONLY_PATHS:
        assert not (third / relative).exists()

def test_public_docs_do_not_reference_private_ci_plumbing(tmp_path: Path):
    output = export_snapshot(tmp_path / "public")
    readme = (output / "README.md").read_text(encoding="utf-8")
    security = (output / "SECURITY.md").read_text(encoding="utf-8")
    combined = readme + "\n" + security

    assert "PUBLIC_PRIVACY_MARKERS" not in combined
    assert "private engineering CI" not in combined.lower()
    assert "docs/PUBLIC_RELEASE_READINESS.md" not in readme

def test_public_workflow_release_permissions_are_isolated(tmp_path: Path):
    output = export_snapshot(tmp_path / "public")
    workflow = (
        output / ".github" / "workflows" / "build-windows.yml"
    ).read_text(encoding="utf-8")

    assert "Package public release files" in workflow
    assert "Upload public release files" in workflow
    assert "Download verified release files" in workflow
    assert "Publish GitHub release" in workflow
    assert "SHA256SUMS.txt" in workflow
    assert "sha256sum -c SHA256SUMS.txt" in workflow
    assert "-notmatch '^\\d+\\.\\d+\\.\\d+$'" in workflow
    assert '[[ ! "$version" =~ ^[0-9]+\\.[0-9]+\\.[0-9]+$ ]]' in workflow
    assert '--notes "Voucher Management $version.' in workflow
    assert "First public stable release of Voucher Management 4.2.0" not in workflow
    assert "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c" in workflow
    assert "github.event.repository.private == false" in workflow


def test_public_release_notes_follow_archive_version(tmp_path: Path):
    output = export_snapshot(tmp_path / "public")
    workflow = (
        output / ".github" / "workflows" / "build-windows.yml"
    ).read_text(encoding="utf-8")

    release_job = workflow.split("\n  release:\n", 1)[1]
    assert 'version="${base#VoucherManagement-}"' in release_job
    assert 'tag="v$version"' in release_job
    assert '--title "Voucher Management $version"' in release_job
    assert '--notes "Voucher Management $version.' in release_job
    assert "CHANGELOG.md" in release_job

