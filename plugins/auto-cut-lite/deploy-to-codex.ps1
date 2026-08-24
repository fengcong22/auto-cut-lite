[CmdletBinding()]
param(
    [switch]$WithAudio,
    [switch]$SkipAudio,
    [switch]$ValidateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$pluginName = 'auto-cut-lite'
$marketplaceName = 'auto-cut-lite-marketplace'
$marketplaceDisplayName = 'Auto-Cut Lite'
$codexNpmPackage = '@openai/codex@0.149.1'
$startedAt = [DateTime]::UtcNow.ToString('o')
$packageRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$userProfile = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
$localAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
$stateRoot = Join-Path $localAppData 'Auto-Cut\auto-cut-lite'
$marketplaceRoot = Join-Path $stateRoot 'marketplace'
$targetParent = Join-Path $marketplaceRoot 'plugins'
$targetRoot = Join-Path $targetParent $pluginName
$marketplacePath = Join-Path $marketplaceRoot '.agents\plugins\marketplace.json'
$personalMarketplacePath = Join-Path $userProfile '.agents\plugins\marketplace.json'
$legacyTargetRoot = Join-Path (Join-Path $userProfile 'plugins') $pluginName
$reportPath = Join-Path $stateRoot 'deployment-report.json'
$stagingRoot = Join-Path $targetParent ('.auto-cut-lite.staging.' + [Guid]::NewGuid().ToString('N'))
$pluginBackup = $null
$marketplaceRegistration = $null
$personalMarketplaceCleanup = $null
$codexEvidence = $null
$marketplaceWasConfigured = $false
$namedPluginWasInstalled = $false
$targetActivated = $false
$oldTargetBackedUp = $false
$reportPathValidated = $false
$sourceIsTarget = [string]::Equals(
    $packageRoot.TrimEnd('\'),
    ([System.IO.Path]::GetFullPath($targetRoot)).TrimEnd('\'),
    [StringComparison]::OrdinalIgnoreCase
)

$report = [ordered]@{
    schema_version = 1
    plugin_name = $pluginName
    plugin_version = $null
    deployment_status = 'failed'
    readiness = 'not_evaluated'
    started_at_utc = $startedAt
    finished_at_utc = $null
    package_root = $packageRoot
    target_root = $targetRoot
    plugin_backup_path = $null
    marketplace_path = $marketplacePath
    marketplace_name = $null
    marketplace_display_name = $marketplaceDisplayName
    marketplace_backup_path = $null
    legacy_personal_marketplace_path = $personalMarketplacePath
    legacy_personal_plugin_path = $legacyTargetRoot
    legacy_personal_plugin_backup_path = $null
    legacy_personal_entry_action = $null
    components = [ordered]@{}
    pending_user_actions = @()
    error = $null
}

function Write-DeploymentReport {
    param([System.Collections.IDictionary]$Payload)
    $Payload.finished_at_utc = [DateTime]::UtcNow.ToString('o')
    New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null
    $temporary = Join-Path $stateRoot ('.deployment-report.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        $json = $Payload | ConvertTo-Json -Depth 12
        [System.IO.File]::WriteAllText($temporary, $json + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $reportPath -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
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
        throw "Path escapes the expected user directory: $candidate"
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
    foreach ($required in @(
        '.codex-plugin/plugin.json',
        'deploy-to-codex.ps1',
        'installer/manage_named_marketplace.py',
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

function Get-CommandEvidence {
    param([Parameter(Mandatory)][string]$Name)
    $command = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $command) {
        return [ordered]@{ status = 'missing'; path = $null }
    }
    return [ordered]@{ status = 'detected'; path = $command.Source }
}

function Resolve-CodexCommand {
    $direct = Get-Command 'codex' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $direct) {
        try {
            & $direct.Source '--version' 2>&1 | Out-Null
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
            & $npx.Source '--version' 2>&1 | Out-Null
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
        & $Evidence.path @Arguments
        return
    }
    if ($Evidence.invocation -eq 'official_npm_fallback') {
        & $Evidence.path '--yes' $Evidence.npm_package @Arguments
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

try {
    if ($WithAudio -and $SkipAudio) {
        throw 'Use either -WithAudio or -SkipAudio, not both. Audio is installed by default.'
    }
    $audioRequested = -not $SkipAudio
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or
        -not [Environment]::Is64BitOperatingSystem -or
        [Runtime.InteropServices.RuntimeInformation]::OSArchitecture -ne [Runtime.InteropServices.Architecture]::X64) {
        throw 'Auto-Cut Lite requires 64-bit Windows on x64 hardware.'
    }
    if ([string]::IsNullOrWhiteSpace($userProfile) -or [string]::IsNullOrWhiteSpace($localAppData)) {
        throw 'Windows user profile paths could not be resolved.'
    }

    Assert-RegularTree -Root $packageRoot
    Assert-NoReparseInExistingPath -Path $targetRoot -StopAt $localAppData
    Assert-NoReparseInExistingPath -Path $marketplacePath -StopAt $localAppData
    Assert-NoReparseInExistingPath -Path $personalMarketplacePath -StopAt $userProfile
    Assert-NoReparseInExistingPath -Path $legacyTargetRoot -StopAt $userProfile
    Assert-NoReparseInExistingPath -Path $reportPath -StopAt $localAppData
    $reportPathValidated = $true
    $packageManifest = Read-AndValidatePackageManifest
    $report.plugin_version = [string]$packageManifest.version

    $pluginManifestPath = Join-Path $packageRoot '.codex-plugin\plugin.json'
    $pluginManifest = Get-Content -LiteralPath $pluginManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($pluginManifest.name -ne $pluginName -or $pluginManifest.version -ne $packageManifest.version) {
        throw 'Plugin manifest identity does not match the package manifest.'
    }
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
        }
        [System.IO.Directory]::Move($stagingRoot, $targetRoot)
        $targetActivated = $true
    }

    $runtimeVenv = Join-Path $targetRoot '.runtime-venv'
    if (-not (Test-Path -LiteralPath (Join-Path $runtimeVenv 'Scripts\python.exe') -PathType Leaf)) {
        & $pythonEvidence.path '-m' 'venv' $runtimeVenv
        if ($LASTEXITCODE -ne 0) {
            throw 'Failed to create the isolated Auto-Cut Python environment.'
        }
    }
    $runtimePython = Join-Path $runtimeVenv 'Scripts\python.exe'
    & $runtimePython '-m' 'pip' 'install' '--disable-pip-version-check' '--upgrade' '-r' (Join-Path $targetRoot 'runtime\requirements.txt')
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to install the Auto-Cut runtime dependencies.'
    }
    & $runtimePython '-m' 'pip' 'check'
    if ($LASTEXITCODE -ne 0) {
        throw 'The isolated Auto-Cut Python environment failed pip check.'
    }
    $report.components.python.runtime_path = $runtimePython
    $report.components.python.dependencies = 'installed'

    $audioVenv = Join-Path $targetRoot 'runtime\.venv-audio'
    $audioPython = Join-Path $audioVenv 'Scripts\python.exe'
    if ($audioRequested) {
        if (-not (Test-Path -LiteralPath $audioPython -PathType Leaf)) {
            & $pythonEvidence.path '-m' 'venv' $audioVenv
            if ($LASTEXITCODE -ne 0) {
                throw 'Failed to create the isolated Auto-Cut audio environment.'
            }
        }
        & $audioPython '-m' 'pip' 'install' '--disable-pip-version-check' '--upgrade' '-r' (Join-Path $targetRoot 'runtime\requirements-audio.lock')
        if ($LASTEXITCODE -ne 0) {
            throw 'Failed to install the isolated audio restoration dependencies.'
        }
        & $audioPython '-m' 'pip' 'check'
        if ($LASTEXITCODE -ne 0) {
            throw 'The isolated Auto-Cut audio environment failed pip check.'
        }
    }
    $report.components.audio_runtime = [ordered]@{
        status = $(if ($audioRequested) { 'installed' } else { 'skipped_by_request' })
        runtime_path = $(if ($audioRequested) { $audioPython } else { $null })
        environment = 'separate'
        requirements = 'runtime/requirements-audio.lock'
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

    $pending = [System.Collections.Generic.List[string]]::new()
    if ($report.components.jianying.status -ne 'detected') { $pending.Add('Install JianYing/CapCut desktop and open it once.') }
    if ($report.components.ffmpeg.status -ne 'detected' -or $report.components.ffprobe.status -ne 'detected') { $pending.Add('Install FFmpeg and FFprobe on PATH.') }
    if ($larkEvidence.status -ne 'detected') { $pending.Add('Install lark-cli, then enable strict Feishu user identity.') }
    elseif (-not $strictUserMode) { $pending.Add('Run lark-cli config default-as user and lark-cli config strict-mode user.') }
    $pending.Add('Authorize Feishu document access as the current user when first prompted.')
    if (-not ($legacyAsr -or $apiKeyAsr)) { $pending.Add('Configure ASR credentials locally on this computer.') }
    elseif ($report.components.asr.validation -ne 'validated') { $pending.Add('Verify ASR credentials with a real alignment request.') }
    if (-not $audioRequested) { $pending.Add('Re-run deployment without -SkipAudio to install the isolated audio runtime.') }

    $report.pending_user_actions = $pending.ToArray()
    $report.deployment_status = 'installed'
    $report.readiness = $(if ($pending.Count -eq 0) { 'ready' } else { 'pending_user_configuration' })
    Write-DeploymentReport -Payload $report

    Write-Host ''
    Write-Host "Auto-Cut Lite $($report.plugin_version) has been installed in Codex."
    Write-Host "deployment_status=$($report.deployment_status)"
    Write-Host "readiness=$($report.readiness)"
    Write-Host "Deployment report: $reportPath"
    Write-Host 'Start a new Codex thread before using the plugin.'
}
catch {
    $originalError = $_.Exception.Message
    $rollbackErrors = [System.Collections.Generic.List[string]]::new()

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
            if ($oldTargetBackedUp -and $null -ne $pluginBackup -and
                (Test-Path -LiteralPath $pluginBackup) -and
                -not (Test-Path -LiteralPath $targetRoot)) {
                [System.IO.Directory]::Move($pluginBackup, $targetRoot)
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

    $report.error = $originalError
    if ($rollbackErrors.Count -gt 0) {
        $report.error = $originalError + ' | ' + ($rollbackErrors -join ' | ')
    }
    if ($reportPathValidated) {
        try { Write-DeploymentReport -Payload $report } catch {}
    }
    throw "Auto-Cut Lite deployment failed: $($report.error)"
}
