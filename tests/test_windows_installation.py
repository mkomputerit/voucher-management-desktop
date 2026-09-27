"""Static deployment-contract checks for the Windows shared installer."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_installer_uses_programfiles_programdata_group_and_sid_acls():
    script = (ROOT / "tools" / "Install-VoucherManagement.ps1").read_text(
        encoding="utf-8"
    )

    assert '[string]$SourcePath = ""' in script
    assert "[string]::IsNullOrWhiteSpace($SourcePath)" in script
    assert "$SourcePath = $PSScriptRoot" in script
    assert "[switch]$ConfigureOnly" in script
    assert '$env:ProgramFiles' in script
    assert '$env:ProgramData' in script
    assert 'Voucher Management Operators' in script
    assert '*S-1-5-18:(OI)(CI)F' in script
    assert '*S-1-5-32-544:(OI)(CI)F' in script
    assert ':(OI)(CI)M' in script
    assert '/reset /T /C' in script
    assert '/inheritance:r' in script
    assert 'Get-Acl' in script
    assert 'principal non autorizzato' in script
    assert 'voucher-management-deployment.json' in script
    assert 'shared_programdata' in script
    assert 'Get-Process -Name "VoucherManagement"' in script
    assert "Everyone" not in script
    assert "Authenticated Users" not in script


def test_uninstaller_preserves_shared_data_without_explicit_switch():
    script = (ROOT / "tools" / "Uninstall-VoucherManagement.ps1").read_text(
        encoding="utf-8"
    )

    assert "[switch]$RemoveData" in script
    assert "if ($RemoveData)" in script
    assert "Dati condivisi conservati" in script
    assert "Remove-LocalGroup" in script
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
    assert "S-1-1-0" in integration
    assert "S-1-5-11" in integration
    assert "RemoveData" in integration



def test_native_setup_preserves_shared_deployment_contract():
    setup = (ROOT / "installer" / "VoucherManagement.iss").read_text(
        encoding="utf-8"
    )

    assert "PrivilegesRequired=admin" in setup
    assert "DefaultDirName={autopf}\\Voucher Management" in setup
    assert "WizardStyle=modern" in setup
    assert "CloseApplications=no" in setup
    assert "RestartApplications=no" in setup
    assert "OPERATORUSER" in setup
    assert "-ConfigureOnly" in setup
    assert "Voucher Management Operators" in setup
    assert "-OperatorGroup" in setup
    assert "Install-VoucherManagement.ps1" in setup
    assert "VoucherManagement-{#AppVersion}-Setup-Windows-x64" in setup
    assert "UninstallDisplayIcon={app}\\{#AppExeName}" in setup
    assert "Get-Process -Name ''VoucherManagement''" in setup


def test_windows_workflow_tracks_native_setup_sources():
    workflow = (ROOT / ".github" / "workflows" / "build-windows.yml").read_text(
        encoding="utf-8"
    )

    assert "- 'installer/**'" in workflow
    assert "Build native Windows Setup" in workflow
    assert "Test native Windows Setup" in workflow
    assert "VoucherManagement-*-Setup-Windows-x64.exe" in workflow
