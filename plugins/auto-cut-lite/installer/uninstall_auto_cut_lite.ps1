[CmdletBinding()]
param(
    [string]$LocalAppDataRoot,
    [string]$UserProfileRoot,
    [switch]$ValidateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module Microsoft.PowerShell.Utility -ErrorAction Stop
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$pluginName = 'auto-cut-lite'
$marketplaceName = 'auto-cut-lite-marketplace'
$codexNpmPackage = '@openai/codex@0.149.1'
$startedAt = [DateTime]::UtcNow.ToString('o')
$localAppDataCandidate = if (-not [string]::IsNullOrWhiteSpace($LocalAppDataRoot)) {
    $LocalAppDataRoot
} elseif (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    $env:LOCALAPPDATA
} else {
    [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
}
$userProfileCandidate = if (-not [string]::IsNullOrWhiteSpace($UserProfileRoot)) {
    $UserProfileRoot
} elseif (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
    $env:USERPROFILE
} else {
    [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
}
$localAppData = [System.IO.Path]::GetFullPath($localAppDataCandidate)
$userProfile = [System.IO.Path]::GetFullPath($userProfileCandidate)
$env:LOCALAPPDATA = $localAppData
$env:USERPROFILE = $userProfile
$stateRoot = Join-Path $localAppData 'Auto-Cut\auto-cut-lite'
$reportPath = Join-Path $stateRoot 'deployment-report.json'
$workspaceReceiptPath = Join-Path $stateRoot 'workspace-install-receipt.json'
$marketplaceRoot = Join-Path $stateRoot 'marketplace'
$marketplacePath = Join-Path $marketplaceRoot '.agents\plugins\marketplace.json'
$targetParent = Join-Path $marketplaceRoot 'plugins'
$targetRoot = Join-Path $targetParent $pluginName
$uninstallReportPath = Join-Path $localAppData 'Auto-Cut\auto-cut-lite-uninstall-report.json'
$marketplaceRemoval = $null
$workspaceResult = $null
$codexEvidence = $null
$pythonCommand = $null
$marketplaceHelper = $null
$pluginWasInstalled = $false
$marketplaceWasConfigured = $false

$uninstallReport = [ordered]@{
    schema_version = 1
    plugin_name = $pluginName
    status = 'failed'
    started_at_utc = $startedAt
    finished_at_utc = $null
    workspace_root = $null
    workspace_unrelated_file_count = 0
    workspace_unrelated_tree_sha256 = $null
    workspace_unrelated_unchanged = $false
    marketplace_unrelated_plugins_sha256 = $null
    marketplace_unrelated_plugins_unchanged = $false
    plugin_registration_removed = $false
    marketplace_registration_removed = $false
    managed_runtime_removed = $false
    error = $null
}

function Write-UninstallReport {
    param([System.Collections.IDictionary]$Payload)
    $Payload.finished_at_utc = [DateTime]::UtcNow.ToString('o')
    $parent = Split-Path -Parent $uninstallReportPath
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $temporary = Join-Path $parent ('.auto-cut-lite-uninstall.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [System.IO.File]::WriteAllText(
            $temporary,
            (($Payload | ConvertTo-Json -Depth 10) + [Environment]::NewLine),
            [System.Text.UTF8Encoding]::new($false)
        )
        Move-Item -LiteralPath $temporary -Destination $uninstallReportPath -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Assert-SafeRoot {
    param([Parameter(Mandatory)][string]$Path)
    if (-not [System.IO.Path]::IsPathRooted($Path)) { throw "Root must be absolute: $Path" }
    $resolved = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    $anchor = [System.IO.Path]::GetPathRoot($resolved).TrimEnd('\')
    if ([string]::Equals($resolved, $anchor, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Root cannot be a filesystem root: $resolved"
    }
    $candidate = $resolved
    while ($candidate.StartsWith($anchor, [StringComparison]::OrdinalIgnoreCase)) {
        if (Test-Path -LiteralPath $candidate) {
            $item = Get-Item -LiteralPath $candidate -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Managed path contains a reparse point: $candidate"
            }
        }
        if ([string]::Equals($candidate, $anchor, [StringComparison]::OrdinalIgnoreCase)) { break }
        $candidate = Split-Path -Parent $candidate
    }
}

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string[]]$Arguments
    )
    $previousErrorActionPreference = $ErrorActionPreference
    $nativeExitCode = 1
    try {
        $ErrorActionPreference = 'Continue'
        & $Path @Arguments
        $nativeExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
        $global:LASTEXITCODE = $nativeExitCode
    }
}

function Resolve-CodexCommand {
    $direct = Get-Command 'codex' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $direct) {
        Invoke-NativeCommand -Path $direct.Source -Arguments @('--version') 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            return [ordered]@{ status = 'detected'; path = $direct.Source; invocation = 'direct'; npm_package = $null }
        }
    }
    $npx = Get-Command 'npx.cmd' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $npx) {
        Invoke-NativeCommand -Path $npx.Source -Arguments @('--version') 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            return [ordered]@{ status = 'detected'; path = $npx.Source; invocation = 'official_npm_fallback'; npm_package = $codexNpmPackage }
        }
    }
    return [ordered]@{ status = 'missing'; path = $null; invocation = $null; npm_package = $null }
}

function Invoke-CodexCommand {
    param(
        [Parameter(Mandatory)][System.Collections.IDictionary]$Evidence,
        [Parameter(Mandatory)][string[]]$Arguments
    )
    if ($Evidence.invocation -eq 'direct') {
        Invoke-NativeCommand -Path $Evidence.path -Arguments $Arguments
        return
    }
    if ($Evidence.invocation -eq 'official_npm_fallback') {
        Invoke-NativeCommand -Path $Evidence.path -Arguments (@('--yes', [string]$Evidence.npm_package) + $Arguments)
        return
    }
    throw 'No usable Codex CLI invocation is available.'
}

function Test-CodexPluginInstalled {
    param([System.Collections.IDictionary]$Evidence, [string]$PluginReference)
    $rows = @(Invoke-CodexCommand -Evidence $Evidence -Arguments @('plugin', 'list') 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "Codex could not list plugins: $($rows -join ' ')" }
    foreach ($row in $rows) {
        if ([string]$row -match ('^\s*' + [regex]::Escape($PluginReference) + '\s+installed(?:,|\s)')) { return $true }
    }
    return $false
}

function Test-CodexMarketplaceConfigured {
    param([System.Collections.IDictionary]$Evidence, [string]$Name, [string]$Root)
    $rows = @(Invoke-CodexCommand -Evidence $Evidence -Arguments @('plugin', 'marketplace', 'list') 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "Codex could not list marketplaces: $($rows -join ' ')" }
    $expected = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    foreach ($row in $rows) {
        $text = [string]$row
        if ($text -match ('^\s*' + [regex]::Escape($Name) + '\s+(.+?)\s*$')) {
            return [string]::Equals(
                ([System.IO.Path]::GetFullPath($Matches[1])).TrimEnd('\'),
                $expected,
                [StringComparison]::OrdinalIgnoreCase
            )
        }
    }
    return $false
}

function Remove-OwnedPluginTree {
    param([Parameter(Mandatory)][string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    $parent = [System.IO.Path]::GetFullPath($targetParent).TrimEnd('\')
    if (-not $resolved.StartsWith($parent + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a plugin outside the managed marketplace: $resolved"
    }
    if (-not (Test-Path -LiteralPath $resolved -PathType Container)) { return }
    $manifestPath = Join-Path $resolved '.codex-plugin\plugin.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Refusing to remove a plugin tree without a manifest: $resolved"
    }
    $identity = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($identity.name -ne $pluginName) {
        throw "Refusing to remove a different plugin tree: $resolved"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}

function Assert-InventoriedHelper {
    param(
        [Parameter(Mandatory)]$Manifest,
        [Parameter(Mandatory)][string]$RelativePath
    )
    $row = @($Manifest.files | Where-Object { [string]::Equals([string]$_.path, $RelativePath, [StringComparison]::OrdinalIgnoreCase) })
    if ($row.Count -ne 1) { throw "Package manifest does not identify exactly one helper: $RelativePath" }
    $path = Join-Path $targetRoot $RelativePath.Replace('/', '\')
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Inventoried helper is missing: $RelativePath" }
    $item = Get-Item -LiteralPath $path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or [int64]$row[0].size -ne $item.Length) {
        throw "Inventoried helper is unsafe or has a size mismatch: $RelativePath"
    }
    $actualHash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne ([string]$row[0].sha256).ToLowerInvariant()) {
        throw "Inventoried helper hash mismatch: $RelativePath"
    }
}

try {
    Assert-SafeRoot -Path $localAppData
    Assert-SafeRoot -Path $userProfile
    Assert-SafeRoot -Path $stateRoot
    if (-not (Test-Path -LiteralPath $reportPath -PathType Leaf)) {
        throw "Auto-Cut Lite deployment report is missing: $reportPath"
    }
    $deployment = Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($deployment.plugin_name -ne $pluginName -or $deployment.deployment_status -ne 'installed') {
        throw 'Deployment report does not identify an installed Auto-Cut Lite runtime.'
    }
    if (-not [string]::Equals(
        [System.IO.Path]::GetFullPath([string]$deployment.target_root).TrimEnd('\'),
        [System.IO.Path]::GetFullPath($targetRoot).TrimEnd('\'),
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'Deployment report target root does not match the managed Auto-Cut Lite path.'
    }
    if (-not (Test-Path -LiteralPath $workspaceReceiptPath -PathType Leaf)) {
        throw "Workspace installation receipt is missing: $workspaceReceiptPath"
    }
    if (-not (Test-Path -LiteralPath $marketplacePath -PathType Leaf)) {
        throw "Named marketplace manifest is missing: $marketplacePath"
    }
    $packageManifestPath = Join-Path $targetRoot 'PACKAGE-MANIFEST.json'
    if (-not (Test-Path -LiteralPath $packageManifestPath -PathType Leaf)) {
        throw "Package manifest is missing: $packageManifestPath"
    }
    $packageManifest = Get-Content -LiteralPath $packageManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($packageManifest.name -ne $pluginName -or $null -eq $packageManifest.files) {
        throw 'Package manifest identity is invalid.'
    }
    Assert-InventoriedHelper -Manifest $packageManifest -RelativePath 'installer/manage_workspace.py'
    Assert-InventoriedHelper -Manifest $packageManifest -RelativePath 'installer/manage_named_marketplace.py'
    $workspaceHelper = Join-Path $targetRoot 'installer\manage_workspace.py'
    $marketplaceHelper = Join-Path $targetRoot 'installer\manage_named_marketplace.py'
    foreach ($helper in @($workspaceHelper, $marketplaceHelper)) {
        if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) { throw "Uninstall helper is missing: $helper" }
    }
    $reportedRuntimePython = [string]$deployment.components.python.runtime_path
    $expectedRuntimePython = Join-Path $targetRoot '.runtime-venv\Scripts\python.exe'
    if (-not [string]::IsNullOrWhiteSpace($reportedRuntimePython)) {
        $resolvedRuntimePython = [System.IO.Path]::GetFullPath($reportedRuntimePython)
        if ([string]::Equals(
            $resolvedRuntimePython,
            [System.IO.Path]::GetFullPath($expectedRuntimePython),
            [StringComparison]::OrdinalIgnoreCase
        ) -and (Test-Path -LiteralPath $resolvedRuntimePython -PathType Leaf)) {
            $runtimePythonItem = Get-Item -LiteralPath $resolvedRuntimePython -Force
            if (($runtimePythonItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) {
                $pythonCommand = [pscustomobject]@{ Source = $resolvedRuntimePython }
            }
        }
    }
    if ($null -eq $pythonCommand) {
        $pythonCommand = Get-Command 'python' -ErrorAction SilentlyContinue | Select-Object -First 1
    }
    if ($null -eq $pythonCommand) { throw 'Python is required to validate and uninstall Auto-Cut Lite.' }
    $codexEvidence = Resolve-CodexCommand
    if ($codexEvidence.status -ne 'detected') { throw 'A usable Codex CLI is required for safe plugin removal.' }
    $pluginReference = $pluginName + '@' + $marketplaceName
    $pluginWasInstalled = Test-CodexPluginInstalled -Evidence $codexEvidence -PluginReference $pluginReference
    $marketplaceWasConfigured = Test-CodexMarketplaceConfigured -Evidence $codexEvidence -Name $marketplaceName -Root $marketplaceRoot

    if ($ValidateOnly) {
        Write-Output 'uninstall_validation=pass'
        Write-Output "state_root=$stateRoot"
        Write-Output "workspace_root=$($deployment.workspace_root)"
        return
    }

    if ($pluginWasInstalled) {
        Invoke-CodexCommand -Evidence $codexEvidence -Arguments @('plugin', 'remove', $pluginReference, '--json') | Out-Null
        if ($LASTEXITCODE -ne 0 -or (Test-CodexPluginInstalled -Evidence $codexEvidence -PluginReference $pluginReference)) {
            throw 'Codex did not remove the Auto-Cut Lite plugin registration.'
        }
        $uninstallReport.plugin_registration_removed = $true
    }

    $marketplaceOutput = & $pythonCommand.Source $marketplaceHelper 'remove-named' `
        '--marketplace-path' $marketplacePath `
        '--json'
    if ($LASTEXITCODE -ne 0) { throw "Named marketplace cleanup failed: $marketplaceOutput" }
    $marketplaceRemoval = $marketplaceOutput | ConvertFrom-Json
    if ($marketplaceRemoval.unrelated_plugins_unchanged -ne $true) {
        throw 'Named marketplace cleanup did not prove unrelated plugin preservation.'
    }
    $uninstallReport.marketplace_unrelated_plugins_sha256 = [string]$marketplaceRemoval.unrelated_plugins_sha256
    $uninstallReport.marketplace_unrelated_plugins_unchanged = $true
    if ([int]$marketplaceRemoval.remaining_plugin_count -eq 0 -and $marketplaceWasConfigured) {
        Invoke-CodexCommand -Evidence $codexEvidence -Arguments @('plugin', 'marketplace', 'remove', $marketplaceName, '--json') | Out-Null
        if ($LASTEXITCODE -ne 0 -or (Test-CodexMarketplaceConfigured -Evidence $codexEvidence -Name $marketplaceName -Root $marketplaceRoot)) {
            throw 'Codex did not remove the empty Auto-Cut Lite marketplace registration.'
        }
        $uninstallReport.marketplace_registration_removed = $true
    }

    $workspaceOutput = & $pythonCommand.Source $workspaceHelper 'uninstall' `
        '--receipt-path' $workspaceReceiptPath `
        '--json'
    if ($LASTEXITCODE -ne 0) { throw "Workspace uninstall failed: $workspaceOutput" }
    $workspaceResult = $workspaceOutput | ConvertFrom-Json
    if ($workspaceResult.status -ne 'uninstalled' -or $workspaceResult.unrelated_unchanged -ne $true) {
        throw 'Workspace uninstall did not prove unrelated file preservation.'
    }
    $uninstallReport.workspace_root = [string]$workspaceResult.workspace_root
    $uninstallReport.workspace_unrelated_file_count = [int]$workspaceResult.unrelated_file_count
    $uninstallReport.workspace_unrelated_tree_sha256 = [string]$workspaceResult.unrelated_tree_sha256
    $uninstallReport.workspace_unrelated_unchanged = $true

    Remove-OwnedPluginTree -Path $targetRoot
    foreach ($backup in @(Get-ChildItem -LiteralPath $targetParent -Directory -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name.StartsWith('.auto-cut-lite.backup.', [StringComparison]::Ordinal) })) {
        Remove-OwnedPluginTree -Path $backup.FullName
    }
    foreach ($ownedDirectory in @(
        'workspace-staging',
        'workspace-backups',
        'dependency-backups',
        'legacy-personal-backups',
        'config'
    )) {
        $ownedPath = Join-Path $stateRoot $ownedDirectory
        if (Test-Path -LiteralPath $ownedPath) { Remove-Item -LiteralPath $ownedPath -Recurse -Force }
    }
    foreach ($ownedFile in @(
        'deployment-report.json',
        'deployment-attempt-report.json',
        'workspace-install-receipt.json',
        'dependency-transaction.json'
    )) {
        $ownedPath = Join-Path $stateRoot $ownedFile
        if (Test-Path -LiteralPath $ownedPath -PathType Leaf) { Remove-Item -LiteralPath $ownedPath -Force }
    }
    if ($null -ne $marketplaceRemoval.marketplace_backup_path -and
        -not [string]::IsNullOrWhiteSpace([string]$marketplaceRemoval.marketplace_backup_path)) {
        $marketplaceBackup = [System.IO.Path]::GetFullPath([string]$marketplaceRemoval.marketplace_backup_path)
        $marketplaceParent = [System.IO.Path]::GetFullPath((Split-Path -Parent $marketplacePath))
        if ((Split-Path -Parent $marketplaceBackup) -ne $marketplaceParent -or
            -not (Split-Path -Leaf $marketplaceBackup).StartsWith('marketplace.json.auto-cut-lite.', [StringComparison]::Ordinal)) {
            throw 'Named marketplace backup path is outside the managed boundary.'
        }
        if (Test-Path -LiteralPath $marketplaceBackup -PathType Leaf) { Remove-Item -LiteralPath $marketplaceBackup -Force }
    }
    if ([int]$marketplaceRemoval.remaining_plugin_count -eq 0) {
        if (Test-Path -LiteralPath $marketplacePath -PathType Leaf) { Remove-Item -LiteralPath $marketplacePath -Force }
    }
    foreach ($candidate in @(
        (Join-Path $marketplaceRoot '.agents\plugins'),
        (Join-Path $marketplaceRoot '.agents'),
        $targetParent,
        $marketplaceRoot,
        $stateRoot
    )) {
        if ((Test-Path -LiteralPath $candidate -PathType Container) -and -not (Get-ChildItem -LiteralPath $candidate -Force | Select-Object -First 1)) {
            [System.IO.Directory]::Delete($candidate, $false)
        }
    }
    $uninstallReport.managed_runtime_removed = $true
    $uninstallReport.status = 'uninstalled'
    Write-UninstallReport -Payload $uninstallReport
    Write-Host 'Auto-Cut Lite has been safely uninstalled.' -ForegroundColor Green
    Write-Host "uninstall_report=$uninstallReportPath"
}
catch {
    $originalError = $_.Exception.Message
    $rollbackErrors = [System.Collections.Generic.List[string]]::new()
    if ($null -eq $workspaceResult -and $null -ne $marketplaceRemoval -and $marketplaceRemoval.changed -and $null -ne $pythonCommand) {
        try {
            & $pythonCommand.Source $marketplaceHelper 'rollback' `
                '--marketplace-path' $marketplacePath `
                '--backup-path' ([string]$marketplaceRemoval.marketplace_backup_path) `
                '--expected-current-sha256' ([string]$marketplaceRemoval.marketplace_sha256) `
                '--json' | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'marketplace rollback returned a failure code' }
        }
        catch { $rollbackErrors.Add("marketplace rollback failed: $($_.Exception.Message)") }
    }
    if ($null -eq $workspaceResult -and $null -ne $codexEvidence -and $marketplaceWasConfigured) {
        try {
            if (-not (Test-CodexMarketplaceConfigured -Evidence $codexEvidence -Name $marketplaceName -Root $marketplaceRoot)) {
                Invoke-CodexCommand -Evidence $codexEvidence -Arguments @('plugin', 'marketplace', 'add', $marketplaceRoot, '--json') | Out-Null
                if ($LASTEXITCODE -ne 0) { throw 'marketplace registration rollback returned a failure code' }
            }
        }
        catch { $rollbackErrors.Add("marketplace registration rollback failed: $($_.Exception.Message)") }
    }
    if ($null -eq $workspaceResult -and $null -ne $codexEvidence -and $pluginWasInstalled) {
        try {
            $pluginReference = $pluginName + '@' + $marketplaceName
            if (-not (Test-CodexPluginInstalled -Evidence $codexEvidence -PluginReference $pluginReference)) {
                Invoke-CodexCommand -Evidence $codexEvidence -Arguments @('plugin', 'add', $pluginReference, '--json') | Out-Null
                if ($LASTEXITCODE -ne 0) { throw 'plugin registration rollback returned a failure code' }
            }
        }
        catch { $rollbackErrors.Add("plugin registration rollback failed: $($_.Exception.Message)") }
    }
    $uninstallReport.error = $originalError
    if ($rollbackErrors.Count -gt 0) {
        $uninstallReport.error = $originalError + ' | ' + ($rollbackErrors -join ' | ')
    }
    try { Write-UninstallReport -Payload $uninstallReport } catch {}
    throw "Auto-Cut Lite uninstall failed: $($uninstallReport.error)"
}
