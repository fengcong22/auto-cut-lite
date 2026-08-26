[CmdletBinding()]
param(
    [switch]$OfficialNetwork,
    [switch]$SkipAudio,
    [switch]$ChooseWorkspace,
    [string]$WorkspaceRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$packageRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$deployer = Join-Path $packageRoot 'deploy-to-codex.ps1'
$manifestPath = Join-Path $packageRoot 'PACKAGE-MANIFEST.json'
$localAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
$reportPath = Join-Path $localAppData 'Auto-Cut\auto-cut-lite\deployment-report.json'
$workspaceReceiptPath = Join-Path $localAppData 'Auto-Cut\auto-cut-lite\workspace-install-receipt.json'

function Write-Step {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host ''
    Write-Host ('[Auto-Cut Lite] ' + $Message) -ForegroundColor Cyan
}

function Get-ExistingWorkspaceRoot {
    if (-not (Test-Path -LiteralPath $workspaceReceiptPath -PathType Leaf)) {
        return $null
    }
    try {
        $receipt = Get-Content -LiteralPath $workspaceReceiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($receipt.status -eq 'installed' -and
            -not [string]::IsNullOrWhiteSpace([string]$receipt.workspace_root) -and
            [System.IO.Path]::IsPathRooted([string]$receipt.workspace_root)) {
            return [System.IO.Path]::GetFullPath([string]$receipt.workspace_root)
        }
    }
    catch {
        # The core deployer performs authoritative receipt validation and reports corruption.
    }
    return $null
}

function Select-WorkspaceRoot {
    param([string]$InitialPath)

    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = 'Choose the Auto-cut-lite workspace folder, or choose its parent folder.'
    $dialog.ShowNewFolderButton = $true
    if (-not [string]::IsNullOrWhiteSpace($InitialPath) -and
        (Test-Path -LiteralPath $InitialPath -PathType Container)) {
        $dialog.SelectedPath = $InitialPath
    }
    $selection = $dialog.ShowDialog()
    if ($selection -ne [System.Windows.Forms.DialogResult]::OK -or
        [string]::IsNullOrWhiteSpace($dialog.SelectedPath)) {
        throw 'Workspace selection was cancelled. Deployment has not started.'
    }

    $selected = [System.IO.Path]::GetFullPath($dialog.SelectedPath)
    $leaf = Split-Path -Leaf $selected.TrimEnd('\', '/')
    if ([string]::Equals($leaf, 'Auto-cut-lite', [StringComparison]::Ordinal)) {
        return $selected
    }
    return [System.IO.Path]::GetFullPath((Join-Path $selected 'Auto-cut-lite'))
}

function Resolve-OneClickWorkspaceRoot {
    if (-not [string]::IsNullOrWhiteSpace($WorkspaceRoot)) {
        if (-not [System.IO.Path]::IsPathRooted($WorkspaceRoot)) {
            throw 'WorkspaceRoot must be an absolute path.'
        }
        return [System.IO.Path]::GetFullPath($WorkspaceRoot)
    }

    $existing = Get-ExistingWorkspaceRoot
    if ($null -ne $existing -and -not $ChooseWorkspace) {
        Add-Type -AssemblyName System.Windows.Forms
        $message = "Existing Auto-Cut Lite workspace:`r`n$existing`r`n`r`nYes: keep and upgrade. No: choose a new workspace. Cancel: exit."
        $answer = [System.Windows.Forms.MessageBox]::Show(
            $message,
            'Auto-Cut Lite Workspace',
            [System.Windows.Forms.MessageBoxButtons]::YesNoCancel,
            [System.Windows.Forms.MessageBoxIcon]::Question
        )
        if ($answer -eq [System.Windows.Forms.DialogResult]::Yes) {
            return $existing
        }
        if ($answer -eq [System.Windows.Forms.DialogResult]::Cancel) {
            throw 'Deployment was cancelled.'
        }
        $initial = Split-Path -Parent $existing
        return Select-WorkspaceRoot -InitialPath $initial
    }

    $initialPath = if ($null -ne $existing) { Split-Path -Parent $existing } else { $packageRoot }
    return Select-WorkspaceRoot -InitialPath $initialPath
}

try {
    Write-Step 'Checking the extracted deployment package...'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $deployer -PathType Leaf)) {
        throw 'The package is incomplete. Use Extract All on the ZIP, then run the launcher from the extracted Auto-cut-lite folder.'
    }

    $selectedWorkspace = Resolve-OneClickWorkspaceRoot
    Write-Step "Selected workspace: $selectedWorkspace"

    $deployArguments = @{ WorkspaceRoot = $selectedWorkspace }
    if (-not $OfficialNetwork) {
        $deployArguments.UseChinaMirrors = $true
        Write-Step 'Using China pip/npm mirrors.'
    }
    else {
        Write-Step 'Using official pip/npm services.'
    }
    if ($SkipAudio) {
        $deployArguments.SkipAudio = $true
    }
    Write-Step 'Installing the runtime, Codex plugin, and workspace skills. Keep this window open...'
    & $deployer @deployArguments

    if (-not (Test-Path -LiteralPath $reportPath -PathType Leaf)) {
        throw "The deployment report was not created: $reportPath"
    }
    $report = Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($report.deployment_status -ne 'installed' -or
        $report.workspace_scope -ne 'repo' -or
        $report.workspace_label -ne 'Auto-cut-lite' -or
        [string]::IsNullOrWhiteSpace([string]$report.workspace_root)) {
        throw 'The deployment report did not pass the Auto-Cut Lite workspace checks.'
    }

    $installedWorkspace = [System.IO.Path]::GetFullPath([string]$report.workspace_root)
    if (-not (Test-Path -LiteralPath $installedWorkspace -PathType Container) -or
        -not (Test-Path -LiteralPath (Join-Path $installedWorkspace 'AGENTS.md') -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $installedWorkspace '.codex\skills\auto-cut\SKILL.md') -PathType Leaf)) {
        throw "The installed workspace is incomplete: $installedWorkspace"
    }

    $clipboardCopied = $false
    try {
        Set-Clipboard -Value $installedWorkspace
        $clipboardCopied = $true
    }
    catch {
        # Clipboard access is optional; the absolute path is still printed below.
    }

    Write-Host ''
    Write-Host '========================================' -ForegroundColor Green
    Write-Host 'Auto-Cut Lite deployment succeeded' -ForegroundColor Green
    Write-Host '========================================' -ForegroundColor Green
    Write-Host "workspace_root=$installedWorkspace"
    Write-Host 'workspace_scope=repo'
    Write-Host 'workspace_label=Auto-cut-lite'
    Write-Host "readiness=$($report.readiness)"
    if ($clipboardCopied) {
        Write-Host 'The workspace path was copied to the clipboard.'
    }
    Write-Host 'Next: open this workspace in Codex, start a new thread, and read CODEX_NEXT_STEPS.md.'

    try {
        $explorerArgument = '"' + $installedWorkspace + '"'
        Start-Process -FilePath 'explorer.exe' -ArgumentList $explorerArgument
    }
    catch {
        Write-Warning "Explorer could not be opened. Open this path manually: $installedWorkspace"
    }
    exit 0
}
catch {
    Write-Host ''
    Write-Host 'Auto-Cut Lite deployment did not complete.' -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ''
    Write-Host 'Keep this window open and send the complete error text above to Codex.'
    if (Test-Path -LiteralPath $reportPath -PathType Leaf) {
        Write-Host "Deployment report: $reportPath"
    }
    exit 1
}
