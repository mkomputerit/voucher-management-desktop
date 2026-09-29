"""Static contract checks for the user-facing Windows Setup.exe."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_nsis_setup_wraps_reviewed_shared_deployment_contract():
    script = (ROOT / "installer" / "VoucherManagement.nsi").read_text(
        encoding="utf-8"
    )

    assert "RequestExecutionLevel admin" in script
    assert '$PROGRAMFILES64\\Voucher Management' in script
    assert '$COMMONAPPDATA\\${PRODUCT_DATA_DIR}' in script
    assert "Install-VoucherManagement.ps1" in script
    assert "Uninstall-VoucherManagement.ps1" in script
    assert "Voucher Management Operators" in script
    assert "WriteUninstaller" in script
    assert "CurrentVersion\\Uninstall\\VoucherManagement" in script
    assert "NoModify" in script
    assert "NoRepair" in script


def test_setup_uninstall_preserves_data_unless_operator_explicitly_purges():
    script = (ROOT / "installer" / "VoucherManagement.nsi").read_text(
        encoding="utf-8"
    )
    uninstall = (ROOT / "tools" / "Uninstall-VoucherManagement.ps1").read_text(
        encoding="utf-8"
    )

    assert "Rimuovi anche tutti i dati condivisi" in script
    assert "/PURGEDATA=" in script
    assert "-RemoveData" in script
    assert "-KeepProgramFiles" in script
    assert "[switch]$KeepProgramFiles" in uninstall
    assert "-not $KeepProgramFiles" in uninstall


def test_setup_ci_pins_and_hash_verifies_nsis_and_runs_real_setup_test():
    workflow = (
        ROOT / ".github" / "workflows" / "build-windows.yml"
    ).read_text(encoding="utf-8")

    assert "NSIS_VERSION: '3.12'" in workflow
    assert "56581f90db321581c5381193d796fffcf2d24b2f8fed2160a6c6a3baa67f2c4f" in workflow
    assert "Build Windows Setup.exe" in workflow
    assert "Test Windows Setup.exe install, upgrade and uninstall" in workflow
    assert "Test-WindowsSetup.ps1" in workflow
    assert "VoucherManagement-*-Setup.exe" in workflow
