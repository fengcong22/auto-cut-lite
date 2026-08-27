[CmdletBinding()]
param(
    [switch]$WithAudio,
    [switch]$SkipAudio,
    [switch]$ValidateOnly,
    [switch]$UseChinaMirrors,
    [string]$WorkspaceRoot,
    [string]$LocalAppDataRoot,
    [string]$UserProfileRoot
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
$marketplaceDisplayName = 'Auto-Cut Lite'
$workspaceLabel = 'Auto-cut-lite'
$expectedWorkspaceSkillCount = 17
$codexNpmPackage = '@openai/codex@0.149.1'
$startedAt = [DateTime]::UtcNow.ToString('o')
$packageRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$userProfileCandidate = if (-not [string]::IsNullOrWhiteSpace($UserProfileRoot)) {
    $UserProfileRoot
} elseif (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
    $env:USERPROFILE
} else {
    [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
}
$localAppDataCandidate = if (-not [string]::IsNullOrWhiteSpace($LocalAppDataRoot)) {
    $LocalAppDataRoot
} elseif (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    $env:LOCALAPPDATA
} else {
    [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
}
$userProfile = [System.IO.Path]::GetFullPath($userProfileCandidate)
$localAppData = [System.IO.Path]::GetFullPath($localAppDataCandidate)
$env:USERPROFILE = $userProfile
$env:LOCALAPPDATA = $localAppData
$stateRoot = Join-Path $localAppData 'Auto-Cut\auto-cut-lite'
$marketplaceRoot = Join-Path $stateRoot 'marketplace'
$targetParent = Join-Path $marketplaceRoot 'plugins'
$targetRoot = Join-Path $targetParent $pluginName
$marketplacePath = Join-Path $marketplaceRoot '.agents\plugins\marketplace.json'
$personalMarketplacePath = Join-Path $userProfile '.agents\plugins\marketplace.json'
$legacyTargetRoot = Join-Path (Join-Path $userProfile 'plugins') $pluginName
$defaultWorkspaceRoot = $packageRoot
$resolvedWorkspaceRoot = $defaultWorkspaceRoot
$workspaceRootSource = 'package_root'
$workspaceSkillsRoot = Join-Path $resolvedWorkspaceRoot '.codex\skills'
$workspaceAgentsPath = Join-Path $resolvedWorkspaceRoot 'AGENTS.md'
$workspaceReceiptPath = Join-Path $stateRoot 'workspace-install-receipt.json'
$reportPath = Join-Path $stateRoot 'deployment-report.json'
$attemptReportPath = Join-Path $stateRoot 'deployment-attempt-report.json'
$pluginManifestInstalledPath = Join-Path $targetRoot '.codex-plugin\plugin.json'
$runtimeRoot = Join-Path $targetRoot 'runtime'
$stagingRoot = Join-Path $targetParent ('.auto-cut-lite.staging.' + [Guid]::NewGuid().ToString('N'))
$pluginBackup = $null
$marketplaceRegistration = $null
$personalMarketplaceCleanup = $null
$workspaceInstall = $null
$pythonEvidence = $null
$dependencyTransaction = $null
$dependencyTransactionCommitted = $false
$codexEvidence = $null
$marketplaceWasConfigured = $false
$namedPluginWasInstalled = $false
$targetActivated = $false
$oldTargetBackedUp = $false
$reportPathValidated = $false
$previousInstalledReport = $null
$workspaceRollbackNeeded = $false
$installedReportDurable = $false
$sourceIsTarget = [string]::Equals(
    $packageRoot.TrimEnd('\'),
    ([System.IO.Path]::GetFullPath($targetRoot)).TrimEnd('\'),
    [StringComparison]::OrdinalIgnoreCase
)

$report = [ordered]@{
    schema_version = 2
    plugin_name = $pluginName
    plugin_version = $null
    deployment_status = 'failed'
    readiness = 'not_evaluated'
    started_at_utc = $startedAt
    finished_at_utc = $null
    package_root = $packageRoot
    target_root = $targetRoot
    plugin_manifest_path = $pluginManifestInstalledPath
    runtime_root = $runtimeRoot
    plugin_backup_path = $null
    plugin_backup_cleanup = 'not_needed'
    plugin_backup_cleanup_error = $null
    plugin_backup_cleanup_removed_count = 0
    plugin_backup_cleanup_deferred = @()
    previous_deployment_report_preserved = $false
    marketplace_path = $marketplacePath
    marketplace_name = $null
    marketplace_display_name = $marketplaceDisplayName
    marketplace_backup_path = $null
    legacy_personal_marketplace_path = $personalMarketplacePath
    legacy_personal_plugin_path = $legacyTargetRoot
    legacy_personal_plugin_backup_path = $null
    legacy_personal_entry_action = $null
    workspace_root = $resolvedWorkspaceRoot
    workspace_root_source = $workspaceRootSource
    workspace_root_customizable = $true
    workspace_root_parameter = 'WorkspaceRoot'
    workspace_mode = 'combined_package_workspace'
    workspace_package_root = $resolvedWorkspaceRoot
    workspace_package_file_count = 0
    workspace_package_sync_action = $null
    workspace_action = $null
    workspace_relocated_from = $null
    workspace_label = $workspaceLabel
    workspace_scope = 'repo'
    workspace_skills_root = $workspaceSkillsRoot
    workspace_agents_path = $workspaceAgentsPath
    workspace_skill_count = 0
    workspace_skill_payload = 'workspace-payload/skills'
    plugin_top_level_skills_present = $false
    workspace_backup_path = $null
    workspace_receipt_path = $workspaceReceiptPath
    workspace_open_required = $true
    china_mirrors_enabled = [bool]$UseChinaMirrors
    components = [ordered]@{}
    pending_user_actions = @()
    error = $null
}

function Write-DeploymentReport {
    param(
        [Parameter(Mandatory)][System.Collections.IDictionary]$Payload,
        [string]$DestinationPath = $reportPath
    )
    $resolvedDestination = [System.IO.Path]::GetFullPath($DestinationPath)
    $allowedDestinations = @(
        [System.IO.Path]::GetFullPath($reportPath),
        [System.IO.Path]::GetFullPath($attemptReportPath)
    )
    if (-not ($allowedDestinations | Where-Object {
        [string]::Equals($_, $resolvedDestination, [StringComparison]::OrdinalIgnoreCase)
    })) {
        throw "Refusing to write an unexpected deployment report: $resolvedDestination"
    }
    $Payload.finished_at_utc = [DateTime]::UtcNow.ToString('o')
    New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null
    $temporary = Join-Path $stateRoot ('.' + [System.IO.Path]::GetFileName($resolvedDestination) + '.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        $json = $Payload | ConvertTo-Json -Depth 12
        [System.IO.File]::WriteAllText($temporary, $json + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $resolvedDestination -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Write-CommittedDeploymentReport {
    param([Parameter(Mandatory)][System.Collections.IDictionary]$Payload)
    $firstFailure = $null
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        try {
            Write-DeploymentReport -Payload $Payload
            return
        }
        catch {
            if ($attempt -eq 1) {
                $firstFailure = $_.Exception.Message
                continue
            }
            throw (
                'The dependency transaction committed, but the installed deployment report ' +
                "could not be written after one retry. First failure: $firstFailure | " +
                "Retry failure: $($_.Exception.Message)"
            )
        }
    }
}

function Test-DependencyTransactionCommitReceipt {
    param([Parameter(Mandatory)][string]$ReceiptPath)
    try {
        $resolvedReceipt = [System.IO.Path]::GetFullPath($ReceiptPath)
        $expectedReceipt = [System.IO.Path]::GetFullPath(
            (Join-Path $stateRoot 'dependency-transaction.json')
        )
        if (-not [string]::Equals(
            $resolvedReceipt,
            $expectedReceipt,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            return $false
        }
        Assert-NoReparseInExistingPath -Path $resolvedReceipt -StopAt $localAppData
        if (-not (Test-Path -LiteralPath $resolvedReceipt -PathType Leaf)) {
            return $false
        }
        $receiptItem = Get-Item -LiteralPath $resolvedReceipt -Force
        if (($receiptItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            $receiptItem.Length -gt 4194304) {
            return $false
        }
        $strictUtf8 = [System.Text.UTF8Encoding]::new($false, $true)
        $receipt = $strictUtf8.GetString(
            [System.IO.File]::ReadAllBytes($resolvedReceipt)
        ) | ConvertFrom-Json
        return [string]$receipt.status -eq 'committed'
    }
    catch {
        return $false
    }
}

function Test-EquivalentDeploymentPath {
    param(
        [Parameter(Mandatory)][string]$SavedPath,
        [Parameter(Mandatory)][string]$ExpectedPath
    )
    try {
        return [string]::Equals(
            [System.IO.Path]::GetFullPath($SavedPath).TrimEnd('\'),
            [System.IO.Path]::GetFullPath($ExpectedPath).TrimEnd('\'),
            [StringComparison]::OrdinalIgnoreCase
        )
    }
    catch {
        return $false
    }
}

function Test-PreservableDeploymentAnchors {
    param(
        [Parameter(Mandatory)]$Payload,
        [Parameter(Mandatory)][string]$PluginVersion
    )
    try {
        $packageManifestPath = Join-Path $targetRoot 'PACKAGE-MANIFEST.json'
        $runtimeIntegrityPath = Join-Path $runtimeRoot 'scripts\utils\runtime_integrity.py'
        $runtimeEntryPath = Join-Path $runtimeRoot 'scripts\jy_wrapper.py'
        $expectedRuntimePython = Join-Path $targetRoot '.runtime-venv\Scripts\python.exe'
        $reportedRuntimePython = [string]$Payload.components.python.runtime_path
        if ([string]$Payload.components.python.status -ne 'detected' -or
            [string]$Payload.components.python.dependencies -ne 'installed' -or
            -not (Test-EquivalentDeploymentPath -SavedPath $reportedRuntimePython -ExpectedPath $expectedRuntimePython)) {
            return $false
        }
        $reportedWorkspaceReceipt = [string]$Payload.workspace_receipt_path
        if (-not (Test-EquivalentDeploymentPath -SavedPath $reportedWorkspaceReceipt -ExpectedPath $workspaceReceiptPath)) {
            return $false
        }
        foreach ($requiredPath in @(
            $packageManifestPath,
            $runtimeIntegrityPath,
            $runtimeEntryPath,
            $expectedRuntimePython,
            $workspaceReceiptPath
        )) {
            $boundary = $(if ($requiredPath -eq $workspaceReceiptPath) { $localAppData } else { $targetParent })
            Assert-NoReparseInExistingPath -Path $requiredPath -StopAt $boundary
            if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
                return $false
            }
            $requiredItem = Get-Item -LiteralPath $requiredPath -Force
            if (($requiredItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                return $false
            }
        }

        $strictUtf8 = [System.Text.UTF8Encoding]::new($false, $true)
        $packageManifestItem = Get-Item -LiteralPath $packageManifestPath -Force
        if ($packageManifestItem.Length -gt 16777216) {
            return $false
        }
        $packageManifest = $strictUtf8.GetString(
            [System.IO.File]::ReadAllBytes($packageManifestPath)
        ) | ConvertFrom-Json
        if ([string]$packageManifest.name -ne $pluginName -or
            [string]$packageManifest.version -ne $PluginVersion -or
            @($packageManifest.files).Count -eq 0) {
            return $false
        }
        foreach ($requiredRelative in @(
            '.codex-plugin/plugin.json',
            'runtime/scripts/utils/runtime_integrity.py',
            'runtime/scripts/jy_wrapper.py'
        )) {
            $rows = @($packageManifest.files | Where-Object {
                [string]$_.path -ieq $requiredRelative
            })
            if ($rows.Count -ne 1) {
                return $false
            }
            $requiredFile = Join-Path $targetRoot $requiredRelative.Replace('/', '\')
            $requiredFileItem = Get-Item -LiteralPath $requiredFile -Force
            $expectedHash = [string]$rows[0].sha256
            if ([int64]$rows[0].size -ne $requiredFileItem.Length -or
                $expectedHash -notmatch '^[0-9a-fA-F]{64}$' -or
                (Get-FileHash -LiteralPath $requiredFile -Algorithm SHA256).Hash -ne $expectedHash) {
                return $false
            }
        }

        $workspaceReceiptItem = Get-Item -LiteralPath $workspaceReceiptPath -Force
        if ($workspaceReceiptItem.Length -gt 16777216) {
            return $false
        }
        $workspaceReceipt = $strictUtf8.GetString(
            [System.IO.File]::ReadAllBytes($workspaceReceiptPath)
        ) | ConvertFrom-Json
        if ([string]$workspaceReceipt.status -ne 'installed' -or
            [string]$workspaceReceipt.plugin_name -ne $pluginName -or
            [string]$workspaceReceipt.plugin_version -ne $PluginVersion -or
            -not (Test-EquivalentDeploymentPath -SavedPath ([string]$workspaceReceipt.deployment_report_path) -ExpectedPath $reportPath) -or
            -not (Test-EquivalentDeploymentPath -SavedPath ([string]$workspaceReceipt.plugin_root) -ExpectedPath $targetRoot) -or
            -not (Test-EquivalentDeploymentPath -SavedPath ([string]$workspaceReceipt.runtime_root) -ExpectedPath $runtimeRoot)) {
            return $false
        }
        $expectedManifestHash = [string]$workspaceReceipt.installed_package_sha256.'PACKAGE-MANIFEST.json'
        if ($expectedManifestHash -notmatch '^[0-9a-fA-F]{64}$' -or
            (Get-FileHash -LiteralPath $packageManifestPath -Algorithm SHA256).Hash -ne $expectedManifestHash) {
            return $false
        }
        return $true
    }
    catch {
        return $false
    }
}

function Get-PreservableDeploymentReport {
    if (-not (Test-Path -LiteralPath $reportPath -PathType Leaf)) {
        return $null
    }
    try {
        Assert-NoReparseInExistingPath -Path $pluginManifestInstalledPath -StopAt $targetParent
        $reportItem = Get-Item -LiteralPath $reportPath -Force -ErrorAction Stop
        if (($reportItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            $reportItem.Length -gt 4194304) {
            return $null
        }
        $bytes = [System.IO.File]::ReadAllBytes($reportPath)
        $strictUtf8 = [System.Text.UTF8Encoding]::new($false, $true)
        $payload = $strictUtf8.GetString($bytes) | ConvertFrom-Json
        if ([int]$payload.schema_version -ne 2 -or
            [string]$payload.plugin_name -ne $pluginName -or
            [string]$payload.deployment_status -ne 'installed') {
            return $null
        }
        if (-not (Test-EquivalentDeploymentPath -SavedPath ([string]$payload.target_root) -ExpectedPath $targetRoot) -or
            -not (Test-EquivalentDeploymentPath -SavedPath ([string]$payload.plugin_manifest_path) -ExpectedPath $pluginManifestInstalledPath) -or
            -not (Test-EquivalentDeploymentPath -SavedPath ([string]$payload.runtime_root) -ExpectedPath $runtimeRoot)) {
            return $null
        }
        if (-not (Test-Path -LiteralPath $pluginManifestInstalledPath -PathType Leaf)) {
            return $null
        }
        $installedManifestItem = Get-Item -LiteralPath $pluginManifestInstalledPath -Force
        if (($installedManifestItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            $installedManifestItem.Length -gt 1048576) {
            return $null
        }
        $installedManifest = Get-Content -LiteralPath $pluginManifestInstalledPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ([string]$installedManifest.name -ne $pluginName -or
            [string]::IsNullOrWhiteSpace([string]$installedManifest.version) -or
            [string]$installedManifest.version -ne [string]$payload.plugin_version) {
            return $null
        }
        if (-not (Test-PreservableDeploymentAnchors `
            -Payload $payload `
            -PluginVersion ([string]$installedManifest.version))) {
            return $null
        }
        return [pscustomobject]@{
            Bytes = $bytes
            PluginVersion = [string]$installedManifest.version
        }
    }
    catch {
        return $null
    }
}

function Restore-PreservedDeploymentReport {
    param([Parameter(Mandatory)]$Snapshot)
    $expectedBytes = [byte[]]$Snapshot.Bytes
    $currentMatches = $false
    if (Test-Path -LiteralPath $reportPath -PathType Leaf) {
        $currentBytes = [System.IO.File]::ReadAllBytes($reportPath)
        if ($currentBytes.Length -eq $expectedBytes.Length) {
            $currentMatches = $true
            for ($index = 0; $index -lt $expectedBytes.Length; $index++) {
                if ($currentBytes[$index] -ne $expectedBytes[$index]) {
                    $currentMatches = $false
                    break
                }
            }
        }
    }
    if ($currentMatches) {
        return
    }
    $temporary = Join-Path $stateRoot ('.deployment-report.restore.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [System.IO.File]::WriteAllBytes($temporary, $expectedBytes)
        Move-Item -LiteralPath $temporary -Destination $reportPath -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
    $restoredBytes = [System.IO.File]::ReadAllBytes($reportPath)
    if ($restoredBytes.Length -ne $expectedBytes.Length) {
        throw 'The restored deployment report length does not match the committed report.'
    }
    for ($index = 0; $index -lt $expectedBytes.Length; $index++) {
        if ($restoredBytes[$index] -ne $expectedBytes[$index]) {
            throw 'The restored deployment report bytes do not match the committed report.'
        }
    }
}

function Test-PreservedPluginIdentity {
    param([Parameter(Mandatory)]$Snapshot)
    try {
        Assert-NoReparseInExistingPath -Path $pluginManifestInstalledPath -StopAt $targetParent
        if (-not (Test-Path -LiteralPath $pluginManifestInstalledPath -PathType Leaf)) {
            return $false
        }
        $manifest = Get-Content -LiteralPath $pluginManifestInstalledPath -Raw -Encoding UTF8 | ConvertFrom-Json
        return [string]$manifest.name -eq $pluginName -and
            [string]$manifest.version -eq [string]$Snapshot.PluginVersion
    }
    catch {
        return $false
    }
}

function Assert-RegularTree {
    param([Parameter(Mandatory)][string]$Root)
    $rootItem = Get-Item -LiteralPath $Root -Force -ErrorAction Stop
    if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Package root cannot be a reparse point: $Root"
    }
    foreach ($item in Get-ChildItem -LiteralPath $Root -Recurse -Force) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Package contains a reparse point: $($item.FullName)"
        }
    }
}

function Assert-NoReparseInExistingPath {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$StopAt
    )
    $candidate = [System.IO.Path]::GetFullPath($Path)
    $boundary = [System.IO.Path]::GetFullPath($StopAt).TrimEnd('\')
    if (-not $candidate.StartsWith($boundary + '\', [StringComparison]::OrdinalIgnoreCase) -and
        -not [string]::Equals($candidate.TrimEnd('\'), $boundary, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escapes the expected boundary: $candidate"
    }
    while ($candidate.StartsWith($boundary, [StringComparison]::OrdinalIgnoreCase)) {
        if (Test-Path -LiteralPath $candidate) {
            $item = Get-Item -LiteralPath $candidate -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Deployment path contains a reparse point: $candidate"
            }
        }
        if ([string]::Equals($candidate.TrimEnd('\'), $boundary, [StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $candidate = Split-Path -Parent $candidate
    }
}

function Resolve-ManifestRelativePath {
    param([Parameter(Mandatory)][string]$RelativePath)
    if ([string]::IsNullOrWhiteSpace($RelativePath) -or [System.IO.Path]::IsPathRooted($RelativePath)) {
        throw "Unsafe package manifest path: $RelativePath"
    }
    $parts = @($RelativePath.Replace('/', '\').Split('\'))
    $unsafeParts = @($parts | Where-Object { $_ -eq '' -or $_ -eq '.' -or $_ -eq '..' })
    if ($parts.Count -eq 0 -or $unsafeParts.Count -gt 0) {
        throw "Unsafe package manifest path: $RelativePath"
    }
    $resolved = [System.IO.Path]::GetFullPath((Join-Path $packageRoot ($parts -join '\')))
    if (-not $resolved.StartsWith($packageRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Package manifest path escapes the package root: $RelativePath"
    }
    return $resolved
}

function Read-AndValidatePackageManifest {
    $manifestPath = Join-Path $packageRoot 'PACKAGE-MANIFEST.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "PACKAGE-MANIFEST.json is missing. Extract the complete ZIP before deployment."
    }
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "PACKAGE-MANIFEST.json is invalid: $($_.Exception.Message)"
    }
    if ($manifest.name -ne $pluginName -or [string]::IsNullOrWhiteSpace([string]$manifest.version)) {
        throw 'Package identity is invalid.'
    }
    if ($null -eq $manifest.files -or @($manifest.files).Count -eq 0) {
        throw 'Package manifest has no file inventory.'
    }
    $seen = @{}
    foreach ($row in $manifest.files) {
        $relative = [string]$row.path
        $key = $relative.ToLowerInvariant()
        if ($seen.ContainsKey($key)) {
            throw "Package manifest has a duplicate path: $relative"
        }
        $seen[$key] = $true
        $source = Resolve-ManifestRelativePath -RelativePath $relative
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Package file is missing: $relative"
        }
        $item = Get-Item -LiteralPath $source -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Package file is a reparse point: $relative"
        }
        if ([int64]$row.size -ne $item.Length) {
            throw "Package file size mismatch: $relative"
        }
        $actualHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne ([string]$row.sha256).ToLowerInvariant()) {
            throw "Package file hash mismatch: $relative"
        }
    }
    $portableContractPath = Join-Path $packageRoot 'PORTABLE-CAPABILITIES.json'
    if (-not (Test-Path -LiteralPath $portableContractPath -PathType Leaf)) {
        throw 'PORTABLE-CAPABILITIES.json is missing.'
    }
    $portableContract = Get-Content -LiteralPath $portableContractPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $workspaceContract = $portableContract.workspace_installation
    foreach ($required in @(
        '.codex-plugin/plugin.json',
        'AGENTS.md',
        'PORTABLE-CAPABILITIES.json',
        [string]$workspaceContract.beginner_guide,
        [string]$workspaceContract.post_install_guide,
        [string]$workspaceContract.one_click_launcher,
        [string]$workspaceContract.one_click_uninstaller,
        'deploy-to-codex.ps1',
        'installer/manage_named_marketplace.py',
        'installer/one_click_deploy.ps1',
        'installer/manage_runtime_dependencies.py',
        'installer/manage_workspace.py',
        'installer/uninstall_auto_cut_lite.ps1',
        'workspace-payload/skills/auto-cut/SKILL.md',
        'runtime/requirements.txt',
        'runtime/requirements-audio.lock'
    )) {
        if (-not $seen.ContainsKey($required.ToLowerInvariant())) {
            throw "Required package file is not inventoried: $required"
        }
    }
    return $manifest
}

function Copy-InventoriedPackage {
    param(
        [Parameter(Mandatory)]$Manifest,
        [Parameter(Mandatory)][string]$Destination
    )
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    foreach ($row in $Manifest.files) {
        $relative = ([string]$row.path).Replace('/', '\')
        $source = Resolve-ManifestRelativePath -RelativePath ([string]$row.path)
        $target = Join-Path $Destination $relative
        $parent = Split-Path -Parent $target
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
        Copy-Item -LiteralPath $source -Destination $target
    }
    Copy-Item -LiteralPath (Join-Path $packageRoot 'PACKAGE-MANIFEST.json') -Destination (Join-Path $Destination 'PACKAGE-MANIFEST.json')
}

function Get-PackagedWorkspaceSkills {
    $skillsRoot = Join-Path $packageRoot 'workspace-payload\skills'
    if (-not (Test-Path -LiteralPath $skillsRoot -PathType Container)) {
        throw 'The package has no workspace skill payload.'
    }
    $skills = @(Get-ChildItem -LiteralPath $skillsRoot -Directory -Force | Where-Object {
        ($_.Name -eq 'auto-cut' -or $_.Name.StartsWith('auto-cut-', [StringComparison]::Ordinal)) -and
        (Test-Path -LiteralPath (Join-Path $_.FullName 'SKILL.md') -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $_.FullName 'agents\openai.yaml') -PathType Leaf)
    })
    if ($skills.Count -ne $expectedWorkspaceSkillCount) {
        throw "Workspace skill payload must contain exactly $expectedWorkspaceSkillCount skills; found $($skills.Count)."
    }
    return $skills
}

function Get-CommandEvidence {
    param([Parameter(Mandatory)][string]$Name)
    $command = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $command) {
        return [ordered]@{ status = 'missing'; path = $null }
    }
    return [ordered]@{ status = 'detected'; path = $command.Source }
}

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string[]]$Arguments
    )
    $previousErrorActionPreference = $ErrorActionPreference
    $nativeExitCode = 1
    try {
        # Windows PowerShell 5.1 wraps native stderr as ErrorRecord objects.
        # Warnings such as `npm notice` must not become terminating errors.
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
        try {
            Invoke-NativeCommand -Path $direct.Source -Arguments @('--version') 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) {
                return [ordered]@{
                    status = 'detected'
                    path = $direct.Source
                    invocation = 'direct'
                    npm_package = $null
                }
            }
        }
        catch {
            # Codex Desktop can expose a packaged binary that normal PowerShell
            # can locate but cannot execute. Fall through to the official npm CLI.
        }
    }

    $npx = Get-Command 'npx.cmd' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $npx) {
        try {
            Invoke-NativeCommand -Path $npx.Source -Arguments @('--version') 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) {
                return [ordered]@{
                    status = 'detected'
                    path = $npx.Source
                    invocation = 'official_npm_fallback'
                    npm_package = $codexNpmPackage
                }
            }
        }
        catch {}
    }

    return [ordered]@{
        status = 'missing_or_unusable'
        path = $null
        invocation = $null
        npm_package = $null
    }
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
        $npxArguments = @('--yes', [string]$Evidence.npm_package) + $Arguments
        Invoke-NativeCommand -Path $Evidence.path -Arguments $npxArguments
        return
    }
    throw 'No usable Codex CLI invocation is available.'
}

function Get-CodexMarketplaceRows {
    param([Parameter(Mandatory)][System.Collections.IDictionary]$Evidence)
    $rows = @(Invoke-CodexCommand -Evidence $Evidence -Arguments @('plugin', 'marketplace', 'list') 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Codex could not list plugin marketplaces: $($rows -join ' ')"
    }
    return $rows
}

function Test-CodexMarketplaceConfigured {
    param(
        [Parameter(Mandatory)][System.Collections.IDictionary]$Evidence,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Root
    )
    $expectedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    $marketplaceRows = @(Get-CodexMarketplaceRows -Evidence $Evidence)
    foreach ($line in $marketplaceRows) {
        $text = [string]$line
        if ($text -match ('^\s*' + [regex]::Escape($Name) + '\s+(.+?)\s*$')) {
            try {
                return [string]::Equals(
                    ([System.IO.Path]::GetFullPath($Matches[1])).TrimEnd('\'),
                    $expectedRoot,
                    [StringComparison]::OrdinalIgnoreCase
                )
            }
            catch { return $false }
        }
    }
    return $false
}

function Test-CodexPluginInstalled {
    param(
        [Parameter(Mandatory)][System.Collections.IDictionary]$Evidence,
        [Parameter(Mandatory)][string]$PluginReference
    )
    $rows = @(Invoke-CodexCommand -Evidence $Evidence -Arguments @('plugin', 'list') 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Codex could not list plugins: $($rows -join ' ')"
    }
    foreach ($line in $rows) {
        $text = [string]$line
        if ($text -match ('^\s*' + [regex]::Escape($PluginReference) + '\s+installed(?:,|\s)')) {
            return $true
        }
    }
    return $false
}

function Find-JianYing {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'JianyingPro\Apps\JianyingPro.exe'),
        (Join-Path $env:LOCALAPPDATA 'CapCut\Apps\CapCut.exe'),
        (Join-Path $env:ProgramFiles 'JianyingPro\JianyingPro.exe'),
        (Join-Path $env:ProgramFiles 'CapCut\CapCut.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return [ordered]@{ status = 'detected'; path = $candidate }
        }
    }
    return [ordered]@{ status = 'missing'; path = $null }
}

function Test-EnvironmentSetting {
    param([Parameter(Mandatory)][string]$Name)
    foreach ($scope in @('Process', 'User', 'Machine')) {
        if (-not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($Name, $scope))) {
            return $true
        }
    }
    return $false
}

function Test-RuntimeEnvSetting {
    param([Parameter(Mandatory)][string]$Name)
    $path = Join-Path $stateRoot 'config\.env'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        return $false
    }
    $item = Get-Item -LiteralPath $path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $item.Length -gt 1048576) {
        return $false
    }
    foreach ($line in Get-Content -LiteralPath $path -Encoding UTF8) {
        if ($line -match ('^\s*' + [regex]::Escape($Name) + '\s*=\s*(.+?)\s*$') -and
            -not [string]::IsNullOrWhiteSpace($Matches[1])) {
            return $true
        }
    }
    return $false
}

function Remove-ExactDeploymentDirectory {
    param([Parameter(Mandatory)][string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    $allowedTarget = [System.IO.Path]::GetFullPath($targetRoot).TrimEnd('\')
    $allowedStaging = [System.IO.Path]::GetFullPath($stagingRoot).TrimEnd('\')
    if (-not [string]::Equals($resolved, $allowedTarget, [StringComparison]::OrdinalIgnoreCase) -and
        -not [string]::Equals($resolved, $allowedStaging, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove an unexpected deployment directory: $resolved"
    }
    if (Test-Path -LiteralPath $resolved) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

function Remove-OwnedPluginBackup {
    param([Parameter(Mandatory)][string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    $expectedParent = [System.IO.Path]::GetFullPath($targetParent).TrimEnd('\')
    $actualParent = [System.IO.Path]::GetDirectoryName($resolved).TrimEnd('\')
    $leaf = [System.IO.Path]::GetFileName($resolved)
    if (-not [string]::Equals($actualParent, $expectedParent, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a plugin backup outside the managed parent: $resolved"
    }
    if ($leaf -notmatch '^\.auto-cut-lite\.backup\.\d{14}\.[0-9a-f]{32}$') {
        throw "Refusing to remove a directory without a managed plugin backup name: $resolved"
    }
    if (-not (Test-Path -LiteralPath $resolved)) {
        return 'not_needed'
    }
    if (-not (Test-Path -LiteralPath $resolved -PathType Container)) {
        throw "Plugin backup is not a directory: $resolved"
    }
    Assert-NoReparseInExistingPath -Path $resolved -StopAt $targetParent
    $directories = [System.Collections.Generic.Stack[string]]::new()
    $directories.Push($resolved)
    while ($directories.Count -gt 0) {
        $directory = $directories.Pop()
        foreach ($item in Get-ChildItem -LiteralPath $directory -Force) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Plugin backup contains a reparse point: $($item.FullName)"
            }
            if ($item.PSIsContainer) {
                $directories.Push($item.FullName)
            }
        }
    }
    $packageManifestPath = Join-Path $resolved 'PACKAGE-MANIFEST.json'
    if (-not (Test-Path -LiteralPath $packageManifestPath -PathType Leaf)) {
        throw "Plugin backup package manifest is missing: $packageManifestPath"
    }
    $packageManifestItem = Get-Item -LiteralPath $packageManifestPath -Force
    if (($packageManifestItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $packageManifestItem.Length -gt 16777216) {
        throw "Plugin backup package manifest is not a regular file: $packageManifestPath"
    }
    $strictUtf8 = [System.Text.UTF8Encoding]::new($false, $true)
    $packageManifest = $strictUtf8.GetString(
        [System.IO.File]::ReadAllBytes($packageManifestPath)
    ) | ConvertFrom-Json
    if ([string]$packageManifest.name -ne $pluginName -or
        [string]::IsNullOrWhiteSpace([string]$packageManifest.version) -or
        @($packageManifest.files).Count -eq 0) {
        throw "Plugin backup package identity is invalid: $resolved"
    }
    $seen = @{}
    foreach ($row in @($packageManifest.files)) {
        $relative = [string]$row.path
        if ([string]::IsNullOrWhiteSpace($relative) -or [System.IO.Path]::IsPathRooted($relative)) {
            throw "Plugin backup package path is unsafe: $relative"
        }
        $parts = @($relative.Replace('/', '\').Split('\'))
        if ($parts.Count -eq 0 -or @($parts | Where-Object { $_ -eq '' -or $_ -eq '.' -or $_ -eq '..' }).Count -gt 0) {
            throw "Plugin backup package path is unsafe: $relative"
        }
        $key = $relative.Replace('\', '/').ToLowerInvariant()
        if ($seen.ContainsKey($key)) {
            throw "Plugin backup package has a duplicate path: $relative"
        }
        $seen[$key] = $true
        $inventoriedPath = [System.IO.Path]::GetFullPath((Join-Path $resolved ($parts -join '\')))
        if (-not $inventoriedPath.StartsWith($resolved + '\', [StringComparison]::OrdinalIgnoreCase) -or
            -not (Test-Path -LiteralPath $inventoriedPath -PathType Leaf)) {
            throw "Plugin backup package file is missing or outside its root: $relative"
        }
        $inventoriedItem = Get-Item -LiteralPath $inventoriedPath -Force
        $expectedHash = [string]$row.sha256
        if (($inventoriedItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            [int64]$row.size -ne $inventoriedItem.Length -or
            $expectedHash -notmatch '^[0-9a-fA-F]{64}$' -or
            (Get-FileHash -LiteralPath $inventoriedPath -Algorithm SHA256).Hash -ne $expectedHash) {
            throw "Plugin backup package file failed inventory validation: $relative"
        }
    }
    foreach ($requiredRelative in @(
        '.codex-plugin/plugin.json',
        'deploy-to-codex.ps1',
        'installer/manage_runtime_dependencies.py'
    )) {
        if (-not $seen.ContainsKey($requiredRelative.ToLowerInvariant())) {
            throw "Plugin backup package is missing a required inventory anchor: $requiredRelative"
        }
    }
    $manifestPath = Join-Path $resolved '.codex-plugin\plugin.json'
    $manifest = $strictUtf8.GetString(
        [System.IO.File]::ReadAllBytes($manifestPath)
    ) | ConvertFrom-Json
    if ([string]$manifest.name -ne $pluginName -or
        [string]$manifest.version -ne [string]$packageManifest.version) {
        throw "Plugin backup manifest identity does not match its package inventory: $resolved"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
    if (Test-Path -LiteralPath $resolved) {
        throw "Plugin backup removal did not complete: $resolved"
    }
    return 'removed'
}

function Remove-OwnedPluginBackups {
    $removed = [System.Collections.Generic.List[string]]::new()
    $deferred = [System.Collections.Generic.List[object]]::new()
    $candidates = @(
        Get-ChildItem -LiteralPath $targetParent -Force -ErrorAction Stop |
            Where-Object {
                $_.Name.StartsWith(
                    '.auto-cut-lite.backup.',
                    [StringComparison]::OrdinalIgnoreCase
                )
            }
    )
    foreach ($candidate in $candidates) {
        try {
            $status = Remove-OwnedPluginBackup -Path $candidate.FullName
            if ($status -eq 'removed') {
                $removed.Add([string]$candidate.FullName)
            }
        }
        catch {
            $deferred.Add([pscustomobject]@{
                path = [string]$candidate.FullName
                error = $_.Exception.Message
            })
        }
    }
    return [pscustomobject]@{
        removed = $removed.ToArray()
        deferred = $deferred.ToArray()
    }
}

try {
    if ($WithAudio -and $SkipAudio) {
        throw 'Use either -WithAudio or -SkipAudio, not both. Audio is installed by default.'
    }
    $audioRequested = -not $SkipAudio
    if ($UseChinaMirrors) {
        $env:PIP_INDEX_URL = 'https://mirrors.aliyun.com/pypi/simple/'
        $env:PIP_DEFAULT_TIMEOUT = '120'
        $env:PIP_RETRIES = '8'
        $env:npm_config_registry = 'https://registry.npmmirror.com'
        $env:npm_config_update_notifier = 'false'
    }
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or
        -not [Environment]::Is64BitOperatingSystem -or
        [Runtime.InteropServices.RuntimeInformation]::OSArchitecture -ne [Runtime.InteropServices.Architecture]::X64) {
        throw 'Auto-Cut Lite requires 64-bit Windows on x64 hardware.'
    }
    if ([string]::IsNullOrWhiteSpace($userProfile) -or [string]::IsNullOrWhiteSpace($localAppData)) {
        throw 'Windows user profile paths could not be resolved.'
    }
    foreach ($deploymentRoot in @($userProfile, $localAppData)) {
        if (-not [System.IO.Path]::IsPathRooted($deploymentRoot)) {
            throw "Deployment roots must be absolute paths: $deploymentRoot"
        }
        $deploymentAnchor = [System.IO.Path]::GetPathRoot($deploymentRoot)
        if ([string]::Equals($deploymentRoot.TrimEnd('\'), $deploymentAnchor.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
            throw "Deployment roots cannot be filesystem roots: $deploymentRoot"
        }
    }

    $previousWorkspaceReceipt = $null
    if (Test-Path -LiteralPath $workspaceReceiptPath -PathType Leaf) {
        try {
            $previousWorkspaceReceipt = Get-Content -LiteralPath $workspaceReceiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
        }
        catch {
            throw "Existing workspace receipt is invalid: $($_.Exception.Message)"
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($WorkspaceRoot)) {
        if (-not [System.IO.Path]::IsPathRooted($WorkspaceRoot)) {
            throw 'WorkspaceRoot must be an absolute path.'
        }
        $resolvedWorkspaceRoot = [System.IO.Path]::GetFullPath($WorkspaceRoot)
        $workspaceRootSource = 'parameter'
    }
    elseif ($null -ne $previousWorkspaceReceipt) {
        if ($previousWorkspaceReceipt.status -eq 'installed' -and
            -not [string]::IsNullOrWhiteSpace([string]$previousWorkspaceReceipt.workspace_root)) {
            $previousWorkspaceRoot = [string]$previousWorkspaceReceipt.workspace_root
            if (-not [System.IO.Path]::IsPathRooted($previousWorkspaceRoot)) {
                throw 'Existing workspace receipt contains a non-absolute workspace root.'
            }
            $resolvedWorkspaceRoot = [System.IO.Path]::GetFullPath($previousWorkspaceRoot)
            $workspaceRootSource = 'existing_receipt'
        }
    }
    $workspaceLeaf = Split-Path -Leaf $resolvedWorkspaceRoot.TrimEnd('\', '/')
    if (-not [string]::Equals($workspaceLeaf, $workspaceLabel, [StringComparison]::Ordinal)) {
        throw "WorkspaceRoot folder name must be exactly: $workspaceLabel"
    }
    $workspaceAnchor = [System.IO.Path]::GetPathRoot($resolvedWorkspaceRoot)
    if ([string]::IsNullOrWhiteSpace($workspaceAnchor)) {
        throw 'WorkspaceRoot has no filesystem anchor.'
    }
    $workspaceComparable = $resolvedWorkspaceRoot.TrimEnd('\')
    $stateComparable = $stateRoot.TrimEnd('\')
    if ([string]::Equals($workspaceComparable, $stateComparable, [StringComparison]::OrdinalIgnoreCase) -or
        $workspaceComparable.StartsWith($stateComparable + '\', [StringComparison]::OrdinalIgnoreCase) -or
        $stateComparable.StartsWith($workspaceComparable + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'WorkspaceRoot cannot overlap the Auto-Cut runtime state root.'
    }
    $workspaceSkillsRoot = Join-Path $resolvedWorkspaceRoot '.codex\skills'
    $workspaceAgentsPath = Join-Path $resolvedWorkspaceRoot 'AGENTS.md'
    $report.workspace_root = $resolvedWorkspaceRoot
    $report.workspace_root_source = $workspaceRootSource
    $report.workspace_skills_root = $workspaceSkillsRoot
    $report.workspace_agents_path = $workspaceAgentsPath
    $report.workspace_package_root = $resolvedWorkspaceRoot

    Assert-RegularTree -Root $packageRoot
    Assert-NoReparseInExistingPath -Path $targetRoot -StopAt $localAppData
    Assert-NoReparseInExistingPath -Path $marketplacePath -StopAt $localAppData
    Assert-NoReparseInExistingPath -Path $personalMarketplacePath -StopAt $userProfile
    Assert-NoReparseInExistingPath -Path $legacyTargetRoot -StopAt $userProfile
    Assert-NoReparseInExistingPath -Path $resolvedWorkspaceRoot -StopAt $workspaceAnchor
    Assert-NoReparseInExistingPath -Path $workspaceReceiptPath -StopAt $localAppData
    Assert-NoReparseInExistingPath -Path $reportPath -StopAt $localAppData
    Assert-NoReparseInExistingPath -Path $attemptReportPath -StopAt $localAppData
    $reportPathValidated = $true
    $previousInstalledReport = Get-PreservableDeploymentReport
    if ($sourceIsTarget -and $null -eq $previousInstalledReport) {
        throw 'Running the deployer from the fixed install directory requires a verified previous installed report.'
    }
    $packageManifest = Read-AndValidatePackageManifest
    $report.plugin_version = [string]$packageManifest.version

    $pluginManifestPath = Join-Path $packageRoot '.codex-plugin\plugin.json'
    $pluginManifest = Get-Content -LiteralPath $pluginManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($pluginManifest.name -ne $pluginName -or $pluginManifest.version -ne $packageManifest.version) {
        throw 'Plugin manifest identity does not match the package manifest.'
    }
    if (@($pluginManifest.PSObject.Properties.Name) -contains 'skills') {
        throw 'Plugin manifest must not expose user-scoped skills; use the workspace skill payload.'
    }
    if (Test-Path -LiteralPath (Join-Path $packageRoot 'skills')) {
        throw 'Plugin package must not contain a top-level skills directory.'
    }
    $packagedWorkspaceSkills = @(Get-PackagedWorkspaceSkills)
    $pythonEvidence = Get-CommandEvidence -Name 'python'
    if ($pythonEvidence.status -ne 'detected') {
        throw 'Python 3.10-3.12 was not found on PATH.'
    }
    # Keep the probe free of quotes and spaces. Windows PowerShell 5.1 otherwise
    # rewrites embedded quotes while constructing a native-process command line.
    $pythonProbe = & $pythonEvidence.path '-c' 'import sys,struct;print(sys.version_info[0],sys.version_info[1],sys.version_info[2],struct.calcsize(chr(80))*8,sep=chr(124))'
    if ($LASTEXITCODE -ne 0) {
        throw 'The detected Python command could not run.'
    }
    $pythonProbeText = [string]($pythonProbe | Select-Object -Last 1)
    $pythonParts = @($pythonProbeText.Trim().Split('|'))
    $pythonMajor = 0
    $pythonMinor = 0
    $pythonPatch = 0
    $pythonBits = 0
    $validPythonProbe = $pythonParts.Count -eq 4 -and
        [int]::TryParse($pythonParts[0], [ref]$pythonMajor) -and
        [int]::TryParse($pythonParts[1], [ref]$pythonMinor) -and
        [int]::TryParse($pythonParts[2], [ref]$pythonPatch) -and
        [int]::TryParse($pythonParts[3], [ref]$pythonBits)
    if (-not $validPythonProbe) {
        throw "The detected Python command returned an invalid runtime probe: $pythonProbeText"
    }
    $pythonVersion = "$pythonMajor.$pythonMinor.$pythonPatch"
    if ($pythonMajor -ne 3 -or $pythonMinor -lt 10 -or $pythonMinor -gt 12 -or $pythonBits -ne 64) {
        throw "Python must be 64-bit version 3.10-3.12; detected $pythonVersion ${pythonBits}-bit."
    }
    if ($audioRequested -and $pythonMinor -ne 11) {
        throw "Full Auto-Cut Lite deployment requires 64-bit Python 3.11 for the isolated audio runtime; detected $pythonVersion."
    }
    $pythonEvidence.version = $pythonVersion
    $pythonEvidence.bits = $pythonBits
    $report.components.python = $pythonEvidence

    $codexEvidence = Resolve-CodexCommand
    $report.components.codex_cli = $codexEvidence
    if ($codexEvidence.status -ne 'detected') {
        throw 'No usable Codex CLI was found. Install Codex CLI, or install Node.js so the official npm CLI fallback can run.'
    }
    $marketplaceWasConfigured = Test-CodexMarketplaceConfigured -Evidence $codexEvidence -Name $marketplaceName -Root $marketplaceRoot
    $namedPluginWasInstalled = Test-CodexPluginInstalled -Evidence $codexEvidence -PluginReference ($pluginName + '@' + $marketplaceName)
    if ($ValidateOnly) {
        Write-Output "package_validation=pass"
        Write-Output "environment_validation=pass"
        Write-Output "plugin_name=$pluginName"
        Write-Output "plugin_version=$($packageManifest.version)"
        Write-Output "python_version=$pythonVersion"
        Write-Output "python_bits=$pythonBits"
        Write-Output "audio_runtime=$(if ($audioRequested) { 'required_separate' } else { 'skipped_by_request' })"
        Write-Output "marketplace_name=$marketplaceName"
        Write-Output "marketplace_display_name=$marketplaceDisplayName"
        Write-Output "workspace_root=$resolvedWorkspaceRoot"
        Write-Output "local_app_data_root=$localAppData"
        Write-Output "user_profile_root=$userProfile"
        Write-Output "workspace_root_source=$workspaceRootSource"
        Write-Output "workspace_root_customizable=true"
        Write-Output "workspace_mode=combined_package_workspace"
        Write-Output "workspace_package_root=$resolvedWorkspaceRoot"
        Write-Output "workspace_upgrade_precedence=parameter_then_existing_receipt_then_package_root"
        Write-Output "workspace_label=$workspaceLabel"
        Write-Output "workspace_scope=repo"
        Write-Output "workspace_skill_count=$($packagedWorkspaceSkills.Count)"
        Write-Output "workspace_skill_payload=workspace-payload/skills"
        Write-Output "plugin_top_level_skills_present=false"
        Write-Output "china_mirrors_enabled=$([bool]$UseChinaMirrors)"
        Write-Output "codex_invocation=$($codexEvidence.invocation)"
        return
    }

    New-Item -ItemType Directory -Path $targetParent -Force | Out-Null
    if (-not $sourceIsTarget) {
        Copy-InventoriedPackage -Manifest $packageManifest -Destination $stagingRoot
        $stagedManifest = Get-Content -LiteralPath (Join-Path $stagingRoot '.codex-plugin\plugin.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($stagedManifest.name -ne $pluginName -or $stagedManifest.version -ne $packageManifest.version) {
            throw 'Staged plugin identity validation failed.'
        }
        if (Test-Path -LiteralPath $targetRoot) {
            $pluginBackup = Join-Path $targetParent ('.auto-cut-lite.backup.' + [DateTime]::UtcNow.ToString('yyyyMMddHHmmss') + '.' + [Guid]::NewGuid().ToString('N'))
            [System.IO.Directory]::Move($targetRoot, $pluginBackup)
            $oldTargetBackedUp = $true
            $report.plugin_backup_path = $pluginBackup
            $report.plugin_backup_cleanup = 'pending'
        }
        [System.IO.Directory]::Move($stagingRoot, $targetRoot)
        $targetActivated = $true
    }

    $dependencyHelper = Join-Path $targetRoot 'installer\manage_runtime_dependencies.py'
    $dependencyArguments = @(
        $dependencyHelper,
        'install',
        '--plugin-root', $targetRoot,
        '--base-python', [string]$pythonEvidence.path,
        '--state-root', $stateRoot,
        '--json'
    )
    if ($oldTargetBackedUp -and $null -ne $pluginBackup) {
        $dependencyArguments += @('--previous-plugin-root', $pluginBackup)
    }
    elseif ($sourceIsTarget) {
        $dependencyArguments += @('--previous-plugin-root', $targetRoot)
    }
    if (-not $audioRequested) {
        $dependencyArguments += '--skip-audio'
    }
    $dependencyOutput = & $pythonEvidence.path @dependencyArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Runtime dependency transaction failed: $dependencyOutput"
    }
    $dependencyTransaction = $dependencyOutput | ConvertFrom-Json
    if ($dependencyTransaction.status -ne 'prepared') {
        throw 'Runtime dependency transaction returned an invalid status.'
    }
    $runtimePython = [string]$dependencyTransaction.environments.main.runtime_path
    $report.components.python.runtime_path = $runtimePython
    $report.components.python.dependencies = 'installed'
    $report.components.python.dependency_action = [string]$dependencyTransaction.environments.main.action
    $report.components.python.requirements_sha256 = [string]$dependencyTransaction.environments.main.lock_sha256
    $report.components.python.pip_check = [string]$dependencyTransaction.environments.main.pip_check

    $audioResult = $dependencyTransaction.environments.audio
    $report.components.audio_runtime = [ordered]@{
        status = $(if ($audioRequested) { 'installed' } else { 'skipped_by_request' })
        action = [string]$audioResult.action
        reason = [string]$audioResult.reason
        runtime_path = $(if ($audioRequested) { [string]$audioResult.runtime_path } else { $null })
        environment = 'separate'
        requirements = 'runtime/requirements-audio.lock'
        requirements_sha256 = $(if ($audioRequested) { [string]$audioResult.lock_sha256 } else { $null })
        pip_check = $(if ($audioRequested) { [string]$audioResult.pip_check } else { $null })
    }

    $larkEvidence = Get-CommandEvidence -Name 'lark-cli'
    $strictUserMode = $false
    if ($larkEvidence.status -eq 'detected') {
        & $larkEvidence.path 'config' 'default-as' 'user' | Out-Null
        $defaultAsExit = $LASTEXITCODE
        & $larkEvidence.path 'config' 'strict-mode' 'user' | Out-Null
        $strictModeExit = $LASTEXITCODE
        $strictUserMode = $defaultAsExit -eq 0 -and $strictModeExit -eq 0
    }
    $larkEvidence.strict_user_mode = $strictUserMode
    $report.components.lark_cli = $larkEvidence
    $report.components.feishu = [ordered]@{
        document_read_identity = 'user'
        strict_user_only = $strictUserMode
        authorization = 'not_verified'
        credentials_bundled = $false
    }

    $legacyAsr = ((Test-EnvironmentSetting -Name 'VOLC_ASR_APP_ID') -or (Test-RuntimeEnvSetting -Name 'VOLC_ASR_APP_ID')) -and
        ((Test-EnvironmentSetting -Name 'VOLC_ASR_ACCESS_TOKEN') -or (Test-RuntimeEnvSetting -Name 'VOLC_ASR_ACCESS_TOKEN'))
    $apiKeyAsr = (Test-EnvironmentSetting -Name 'VOLC_ASR_API_KEY') -or (Test-RuntimeEnvSetting -Name 'VOLC_ASR_API_KEY')
    $report.components.asr = [ordered]@{
        configuration_detected = ($legacyAsr -or $apiKeyAsr)
        mode = $(if ($apiKeyAsr) { 'new_console_api_key' } elseif ($legacyAsr) { 'legacy_app_id_access_token' } else { 'missing' })
        credentials_bundled = $false
        validation = $(if ($legacyAsr -or $apiKeyAsr) { 'configuration_present_unverified' } else { 'pending_user_configuration' })
    }
    $report.components.jianying = Find-JianYing
    $report.components.ffmpeg = Get-CommandEvidence -Name 'ffmpeg'
    $report.components.ffprobe = Get-CommandEvidence -Name 'ffprobe'

    $helper = Join-Path $targetRoot 'installer\manage_named_marketplace.py'
    $registrationOutput = & $pythonEvidence.path $helper 'register-named' '--plugin-dir' $targetRoot '--marketplace-root' $marketplaceRoot '--json'
    if ($LASTEXITCODE -ne 0) {
        throw "Named marketplace registration failed: $registrationOutput"
    }
    $marketplaceRegistration = $registrationOutput | ConvertFrom-Json
    $report.marketplace_name = $marketplaceRegistration.marketplace_name
    $report.marketplace_display_name = $marketplaceRegistration.marketplace_display_name
    $report.marketplace_backup_path = $marketplaceRegistration.marketplace_backup_path

    if (-not (Test-CodexMarketplaceConfigured -Evidence $codexEvidence -Name $marketplaceName -Root $marketplaceRoot)) {
        $marketplaceAddOutput = @(Invoke-CodexCommand -Evidence $codexEvidence -Arguments @('plugin', 'marketplace', 'add', $marketplaceRoot, '--json') 2>&1)
        if ($LASTEXITCODE -ne 0 -and
            -not (Test-CodexMarketplaceConfigured -Evidence $codexEvidence -Name $marketplaceName -Root $marketplaceRoot)) {
            throw "Codex rejected the named marketplace: $($marketplaceAddOutput -join ' ')"
        }
    }
    if (-not (Test-CodexMarketplaceConfigured -Evidence $codexEvidence -Name $marketplaceName -Root $marketplaceRoot)) {
        throw 'Codex did not retain the Auto-Cut Lite marketplace registration.'
    }

    $pluginReference = $pluginName + '@' + $marketplaceName
    Invoke-CodexCommand -Evidence $codexEvidence -Arguments @('plugin', 'add', $pluginReference, '--json')
    if ($LASTEXITCODE -ne 0) {
        throw 'Codex rejected the Auto-Cut Lite plugin installation command.'
    }
    if (-not (Test-CodexPluginInstalled -Evidence $codexEvidence -PluginReference $pluginReference)) {
        throw 'Codex did not report the named-marketplace plugin as installed.'
    }

    $legacyReference = $pluginName + '@personal'
    if (Test-CodexPluginInstalled -Evidence $codexEvidence -PluginReference $legacyReference) {
        $legacyRemoveOutput = @(Invoke-CodexCommand -Evidence $codexEvidence -Arguments @('plugin', 'remove', $legacyReference, '--json') 2>&1)
        if ($LASTEXITCODE -ne 0 -or (Test-CodexPluginInstalled -Evidence $codexEvidence -PluginReference $legacyReference)) {
            throw "Codex could not remove the legacy personal installation: $($legacyRemoveOutput -join ' ')"
        }
    }
    $cleanupOutput = & $pythonEvidence.path $helper 'remove-personal' '--marketplace-path' $personalMarketplacePath '--json'
    if ($LASTEXITCODE -ne 0) {
        throw "Legacy personal marketplace cleanup failed: $cleanupOutput"
    }
    $personalMarketplaceCleanup = $cleanupOutput | ConvertFrom-Json
    $report.legacy_personal_entry_action = $personalMarketplaceCleanup.entry_action

    if (Test-Path -LiteralPath $legacyTargetRoot -PathType Container) {
        $legacyManifest = Join-Path $legacyTargetRoot '.codex-plugin\plugin.json'
        if (-not (Test-Path -LiteralPath $legacyManifest -PathType Leaf)) {
            throw "Refusing to move an unverified legacy plugin directory: $legacyTargetRoot"
        }
        $legacyIdentity = Get-Content -LiteralPath $legacyManifest -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($legacyIdentity.name -ne $pluginName) {
            throw "Refusing to move a different legacy plugin directory: $legacyTargetRoot"
        }
        $legacyBackupRoot = Join-Path $stateRoot 'legacy-personal-backups'
        New-Item -ItemType Directory -Path $legacyBackupRoot -Force | Out-Null
        $legacyBackup = Join-Path $legacyBackupRoot ('auto-cut-lite.' + [DateTime]::UtcNow.ToString('yyyyMMddHHmmss') + '.' + [Guid]::NewGuid().ToString('N'))
        [System.IO.Directory]::Move($legacyTargetRoot, $legacyBackup)
        $report.legacy_personal_plugin_backup_path = $legacyBackup
    }

    $workspaceHelper = Join-Path $targetRoot 'installer\manage_workspace.py'
    $workspaceOutput = & $pythonEvidence.path $workspaceHelper 'install' `
        '--plugin-root' $targetRoot `
        '--workspace-root' $resolvedWorkspaceRoot `
        '--state-root' $stateRoot `
        '--receipt-path' $workspaceReceiptPath `
        '--json'
    if ($LASTEXITCODE -ne 0) {
        throw "Workspace skill installation failed: $workspaceOutput"
    }
    $workspaceRollbackNeeded = $true
    $workspaceInstall = $workspaceOutput | ConvertFrom-Json
    if ($workspaceInstall.status -ne 'installed' -or
        $workspaceInstall.workspace_scope -ne 'repo' -or
        $workspaceInstall.workspace_label -ne $workspaceLabel -or
        $workspaceInstall.workspace_mode -ne 'combined_package_workspace' -or
        -not [string]::Equals([string]$workspaceInstall.workspace_package_root, $resolvedWorkspaceRoot, [StringComparison]::OrdinalIgnoreCase) -or
        [int]$workspaceInstall.workspace_package_file_count -le 0 -or
        [int]$workspaceInstall.workspace_skill_count -ne $expectedWorkspaceSkillCount -or
        $workspaceInstall.plugin_manifest_exposes_skills -ne $false -or
        $workspaceInstall.plugin_top_level_skills_present -ne $false -or
        $workspaceInstall.workspace_skill_payload -ne 'workspace-payload/skills') {
        throw 'Workspace skill installation receipt failed validation.'
    }
    $report.workspace_skill_count = [int]$workspaceInstall.workspace_skill_count
    $report.workspace_package_file_count = [int]$workspaceInstall.workspace_package_file_count
    $report.workspace_package_sync_action = [string]$workspaceInstall.package_sync_action
    $report.workspace_backup_path = [string]$workspaceInstall.backup_root
    $report.workspace_action = [string]$workspaceInstall.workspace_action
    if (@($workspaceInstall.PSObject.Properties.Name) -contains 'relocated_from_workspace_root' -and
        -not [string]::IsNullOrWhiteSpace([string]$workspaceInstall.relocated_from_workspace_root)) {
        $report.workspace_relocated_from = [string]$workspaceInstall.relocated_from_workspace_root
    }

    $pending = [System.Collections.Generic.List[string]]::new()
    if ($report.components.jianying.status -ne 'detected') { $pending.Add('Install JianYing/CapCut desktop and open it once.') }
    if ($report.components.ffmpeg.status -ne 'detected' -or $report.components.ffprobe.status -ne 'detected') { $pending.Add('Install FFmpeg and FFprobe on PATH.') }
    if ($larkEvidence.status -ne 'detected') { $pending.Add('Install lark-cli, then enable strict Feishu user identity.') }
    elseif (-not $strictUserMode) { $pending.Add('Run lark-cli config default-as user and lark-cli config strict-mode user.') }
    $pending.Add('Authorize Feishu document access as the current user when first prompted.')
    if (-not ($legacyAsr -or $apiKeyAsr)) { $pending.Add('Configure ASR credentials locally on this computer.') }
    elseif ($report.components.asr.validation -ne 'validated') { $pending.Add('Verify ASR credentials with a real alignment request.') }
    if (-not $audioRequested) { $pending.Add('Re-run deployment without -SkipAudio to install the isolated audio runtime.') }
    $pending.Add("Open the Auto-Cut Lite workspace in Codex and start a new thread: $resolvedWorkspaceRoot")

    $report.pending_user_actions = $pending.ToArray()
    $installedReadiness = $(if ($pending.Count -eq 0) { 'ready' } else { 'pending_user_configuration' })
    $report.deployment_status = 'dependency_commit_pending'
    $report.readiness = 'not_evaluated'
    Write-DeploymentReport -Payload $report -DestinationPath $attemptReportPath
    $dependencyCommitOutput = & $pythonEvidence.path $dependencyHelper 'commit' `
        '--receipt-path' ([string]$dependencyTransaction.transaction_receipt_path) `
        '--state-root' $stateRoot `
        '--json'
    $dependencyCommitExitCode = $LASTEXITCODE
    $dependencyTransactionCommitted = Test-DependencyTransactionCommitReceipt `
        -ReceiptPath ([string]$dependencyTransaction.transaction_receipt_path)
    if ($dependencyCommitExitCode -ne 0) {
        throw "Runtime dependency transaction commit failed: $dependencyCommitOutput"
    }
    $dependencyCommit = $dependencyCommitOutput | ConvertFrom-Json
    if ($dependencyCommit.status -ne 'committed') {
        throw 'Runtime dependency transaction commit returned an invalid status.'
    }
    if (-not $dependencyTransactionCommitted) {
        throw 'Runtime dependency transaction receipt did not confirm a committed state.'
    }
    $report.deployment_status = 'installed'
    $report.readiness = $installedReadiness
    Write-CommittedDeploymentReport -Payload $report
    $installedReportDurable = $true

    try {
        $backupCleanup = Remove-OwnedPluginBackups
        $removedBackupCount = @($backupCleanup.removed).Count
        $deferredBackups = @($backupCleanup.deferred)
        $report.plugin_backup_cleanup_removed_count = $removedBackupCount
        $report.plugin_backup_cleanup_deferred = $deferredBackups
        if ($deferredBackups.Count -gt 0) {
            $report.plugin_backup_cleanup = 'deferred'
            $report.plugin_backup_cleanup_error = @(
                $deferredBackups | ForEach-Object { "$($_.path): $($_.error)" }
            ) -join ' | '
        }
        elseif ($removedBackupCount -gt 0) {
            $report.plugin_backup_cleanup = 'removed'
            $report.plugin_backup_cleanup_error = $null
        }
        else {
            $report.plugin_backup_cleanup = 'not_needed'
            $report.plugin_backup_cleanup_error = $null
        }
        if ($null -ne $pluginBackup -and -not (Test-Path -LiteralPath $pluginBackup)) {
            $report.plugin_backup_path = $null
        }
    }
    catch {
        $report.plugin_backup_cleanup = 'deferred'
        $report.plugin_backup_cleanup_error = $_.Exception.Message
        $report.plugin_backup_cleanup_deferred = @(
            [pscustomobject]@{
                path = $targetParent
                error = $_.Exception.Message
            }
        )
    }
    try {
        Write-DeploymentReport -Payload $report
    }
    catch {
        Write-Warning "The committed deployment succeeded, but its backup-cleanup report could not be refreshed: $($_.Exception.Message)"
    }
    if (Test-Path -LiteralPath $attemptReportPath -PathType Leaf) {
        try {
            Remove-Item -LiteralPath $attemptReportPath -Force
        }
        catch {
            throw "The committed deployment succeeded, but its pending attempt report could not be removed: $($_.Exception.Message)"
        }
    }

    Write-Host ''
    Write-Host "Auto-Cut Lite $($report.plugin_version) has been installed in Codex."
    Write-Host "deployment_status=$($report.deployment_status)"
    Write-Host "readiness=$($report.readiness)"
    Write-Host "workspace_root=$resolvedWorkspaceRoot"
    Write-Host "workspace_root_source=$workspaceRootSource"
    Write-Host "workspace_scope=repo"
    Write-Host "workspace_mode=combined_package_workspace"
    Write-Host "workspace_package_file_count=$($report.workspace_package_file_count)"
    Write-Host "workspace_package_sync_action=$($report.workspace_package_sync_action)"
    Write-Host "workspace_label=$workspaceLabel"
    Write-Host "workspace_skill_count=$($report.workspace_skill_count)"
    Write-Host "Deployment report: $reportPath"
    Write-Host "Open this folder in Codex, then start a new thread: $resolvedWorkspaceRoot"
}
catch {
    $originalError = $_.Exception.Message
    $rollbackErrors = [System.Collections.Generic.List[string]]::new()

    if ($dependencyTransactionCommitted) {
        $report.deployment_status = $(if ($installedReportDurable) { 'installed' } else { 'installed_report_pending' })
        $report.readiness = $(if ($installedReportDurable) { $report.readiness } else { 'not_evaluated' })
        $report.error = $originalError
        $report.previous_deployment_report_preserved = $false
        if (-not $installedReportDurable) {
            $report.plugin_backup_cleanup = 'deferred'
            $report.plugin_backup_cleanup_error = 'Deployment committed before the installed report became durable.'
        }
        try {
            Write-DeploymentReport -Payload $report -DestinationPath $attemptReportPath
        }
        catch {
            Write-Warning "The committed deployment report remains pending and its attempt report could not be refreshed: $($_.Exception.Message)"
        }
        throw "Auto-Cut Lite deployment committed, but report finalization failed: $originalError"
    }

    if ($null -ne $dependencyTransaction -and -not $dependencyTransactionCommitted -and $null -ne $pythonEvidence) {
        try {
            $dependencyHelperForRollback = Join-Path $packageRoot 'installer\manage_runtime_dependencies.py'
            & $pythonEvidence.path $dependencyHelperForRollback 'rollback' `
                '--receipt-path' ([string]$dependencyTransaction.transaction_receipt_path) `
                '--state-root' $stateRoot `
                '--json' | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'dependency rollback returned a failure code' }
        }
        catch { $rollbackErrors.Add("dependency rollback failed: $($_.Exception.Message)") }
    }

    if ($workspaceRollbackNeeded -and $null -ne $pythonEvidence) {
        try {
            $workspaceHelperForRollback = Join-Path $packageRoot 'installer\manage_workspace.py'
            & $pythonEvidence.path $workspaceHelperForRollback 'rollback' `
                '--receipt-path' $workspaceReceiptPath `
                '--json' | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'workspace rollback returned a failure code' }
            $workspaceRollbackNeeded = $false
        }
        catch { $rollbackErrors.Add("workspace rollback failed: $($_.Exception.Message)") }
    }

    if ($null -ne $codexEvidence -and -not $namedPluginWasInstalled) {
        try {
            $namedReference = $pluginName + '@' + $marketplaceName
            if (Test-CodexPluginInstalled -Evidence $codexEvidence -PluginReference $namedReference) {
                Invoke-CodexCommand -Evidence $codexEvidence -Arguments @('plugin', 'remove', $namedReference, '--json') | Out-Null
                if ($LASTEXITCODE -ne 0) { throw 'Codex plugin remove returned a failure code' }
            }
        }
        catch { $rollbackErrors.Add("Codex plugin rollback failed: $($_.Exception.Message)") }
    }

    if ($null -ne $personalMarketplaceCleanup -and $personalMarketplaceCleanup.changed) {
        try {
            $helperForRollback = Join-Path $packageRoot 'installer\manage_named_marketplace.py'
            $rollbackArguments = @(
                $helperForRollback,
                'rollback',
                '--marketplace-path', $personalMarketplacePath,
                '--expected-current-sha256', [string]$personalMarketplaceCleanup.marketplace_sha256,
                '--backup-path', [string]$personalMarketplaceCleanup.marketplace_backup_path,
                '--json'
            )
            & $pythonEvidence.path @rollbackArguments | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'personal marketplace rollback returned a failure code' }
        }
        catch { $rollbackErrors.Add("personal marketplace rollback failed: $($_.Exception.Message)") }
    }

    if ($null -ne $marketplaceRegistration -and $marketplaceRegistration.changed) {
        try {
            $helperForRollback = Join-Path $packageRoot 'installer\manage_named_marketplace.py'
            $rollbackArguments = @(
                $helperForRollback,
                'rollback',
                '--marketplace-path', $marketplacePath,
                '--expected-current-sha256', [string]$marketplaceRegistration.marketplace_sha256,
                '--json'
            )
            if ($marketplaceRegistration.marketplace_created) {
                $rollbackArguments += '--created-new'
            }
            else {
                $rollbackArguments += @('--backup-path', [string]$marketplaceRegistration.marketplace_backup_path)
            }
            & $pythonEvidence.path @rollbackArguments | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'marketplace helper returned a failure code' }
        }
        catch {
            $rollbackErrors.Add("marketplace rollback failed: $($_.Exception.Message)")
        }
    }

    if ($null -ne $codexEvidence -and -not $marketplaceWasConfigured) {
        try {
            if (Test-CodexMarketplaceConfigured -Evidence $codexEvidence -Name $marketplaceName -Root $marketplaceRoot) {
                Invoke-CodexCommand -Evidence $codexEvidence -Arguments @('plugin', 'marketplace', 'remove', $marketplaceName, '--json') | Out-Null
                if ($LASTEXITCODE -ne 0) { throw 'Codex marketplace remove returned a failure code' }
            }
        }
        catch { $rollbackErrors.Add("Codex marketplace rollback failed: $($_.Exception.Message)") }
    }

    if (-not $sourceIsTarget) {
        try {
            if ($targetActivated) {
                Remove-ExactDeploymentDirectory -Path $targetRoot
            }
            if ($oldTargetBackedUp -and $null -ne $pluginBackup) {
                if (-not (Test-Path -LiteralPath $pluginBackup -PathType Container)) {
                    throw "The previous plugin backup is missing: $pluginBackup"
                }
                if (Test-Path -LiteralPath $targetRoot) {
                    throw "The failed plugin target still exists and blocks rollback: $targetRoot"
                }
                [System.IO.Directory]::Move($pluginBackup, $targetRoot)
                $report.plugin_backup_cleanup = 'restored'
            }
        }
        catch {
            $rollbackErrors.Add("plugin rollback failed: $($_.Exception.Message)")
        }
    }
    if (Test-Path -LiteralPath $stagingRoot) {
        try { Remove-ExactDeploymentDirectory -Path $stagingRoot }
        catch { $rollbackErrors.Add("staging cleanup failed: $($_.Exception.Message)") }
    }
    if ($null -ne $previousInstalledReport -and
        -not (Test-PreservedPluginIdentity -Snapshot $previousInstalledReport)) {
        $rollbackErrors.Add('previous plugin identity was not restored')
    }

    $report.deployment_status = 'failed'
    $report.readiness = 'not_evaluated'
    if ($reportPathValidated) {
        $preservePreviousReport = $null -ne $previousInstalledReport -and $rollbackErrors.Count -eq 0
        if ($preservePreviousReport) {
            try {
                Restore-PreservedDeploymentReport -Snapshot $previousInstalledReport
            }
            catch {
                $rollbackErrors.Add("deployment report restore failed: $($_.Exception.Message)")
                $preservePreviousReport = $false
            }
        }
        $report.error = $originalError
        if ($rollbackErrors.Count -gt 0) {
            $report.error = $originalError + ' | ' + ($rollbackErrors -join ' | ')
        }
        $report.previous_deployment_report_preserved = $preservePreviousReport
        try { Write-DeploymentReport -Payload $report -DestinationPath $attemptReportPath } catch {}
        if (-not $preservePreviousReport) {
            try { Write-DeploymentReport -Payload $report -DestinationPath $reportPath } catch {}
        }
    }
    else {
        $report.error = $originalError
        if ($rollbackErrors.Count -gt 0) {
            $report.error = $originalError + ' | ' + ($rollbackErrors -join ' | ')
        }
    }
    throw "Auto-Cut Lite deployment failed: $($report.error)"
}
