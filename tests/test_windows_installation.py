"""Static deployment-contract checks for the Windows shared installer."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_installer_uses_programfiles_programdata_and_builtin_users_acl():
    script = (ROOT / "tools" / "Install-VoucherManagement.ps1").read_text(
        encoding="utf-8"
    )

    assert '$SourcePath = $PSScriptRoot' in script
    assert '$env:ProgramFiles' in script
    assert '$env:ProgramData' in script
    assert 'S-1-5-32-545' in script
    assert '*S-1-5-18:(OI)(CI)F' in script
    assert '*S-1-5-32-544:(OI)(CI)F' in script
    assert ':(OI)(CI)M' in script
    assert '/reset /T /C' in script
    assert '/inheritance:r' in script
    assert 'Get-Acl' in script
    assert 'principal non autorizzato' in script
    assert 'voucher-management-deployment.json' in script
    assert 'shared_programdata' in script
    assert 'builtin_users_modify' in script
    assert 'Get-Process -Name "VoucherManagement"' in script
    assert "Resolve-InteractiveUser" not in script
    assert "OperatorUser" not in script
    assert "New-LocalGroup" not in script
    assert "Everyone" not in script
    assert "Authenticated Users" not in script


def test_uninstaller_preserves_shared_data_without_explicit_switch():
    script = (ROOT / "tools" / "Uninstall-VoucherManagement.ps1").read_text(
        encoding="utf-8"
    )

    assert "[switch]$RemoveData" in script
    assert "if ($RemoveData)" in script
    assert "Dati condivisi conservati" in script
    assert "Remove-LocalGroup" not in script
    assert "OperatorGroup" not in script
    assert "icacls.exe $DataRoot /reset /T /C" in script
    assert 'Get-Process -Name "VoucherManagement"' in script


def test_windows_build_packages_installer_and_uninstaller():
    workflow = (ROOT / ".github" / "workflows" / "build-windows.yml").read_text(
        encoding="utf-8"
    )

    assert (
        "Copy-Item tools\\Install-VoucherManagement.ps1 "
        "dist\\VoucherManagement\\Install-VoucherManagement.ps1"
    ) in workflow
    assert (
        "Copy-Item tools\\Uninstall-VoucherManagement.ps1 "
        "dist\\VoucherManagement\\Uninstall-VoucherManagement.ps1"
    ) in workflow


def test_windows_ci_executes_real_installer_acl_integration():
    workflow = (ROOT / ".github" / "workflows" / "build-windows.yml").read_text(
        encoding="utf-8"
    )
    integration = (
        ROOT / "tools" / "Test-WindowsSharedInstall.ps1"
    ).read_text(encoding="utf-8")

    assert "Test shared Windows installer and ACLs" in workflow
    assert "Test-WindowsSharedInstall.ps1" in workflow
    assert "Get-Acl" in integration
    assert "AreAccessRulesProtected" in integration
    assert "S-1-5-32-545" in integration
    assert "S-1-1-0" in integration
    assert "S-1-5-11" in integration
    assert "RemoveData" in integration
