[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$InstallerArgs
)

$ErrorActionPreference = "Stop"

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    [Console]::Error.WriteLine("Auto-Cut setup requires Windows 10 or Windows 11.")
    exit 2
}

if ([Environment]::OSVersion.Version.Major -lt 10) {
    [Console]::Error.WriteLine("Auto-Cut setup requires Windows 10 or Windows 11.")
    exit 2
}

if (-not [Environment]::Is64BitOperatingSystem) {
    [Console]::Error.WriteLine("Auto-Cut setup requires a 64-bit Windows operating system.")
    exit 2
}

function Test-PythonCandidate {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,
        [string[]]$PrefixArgs = @()
    )

    $probeScript = @'
import json
import platform
import sys
print(json.dumps({
    'version': list(sys.version_info[:3]),
    'bits': platform.architecture()[0],
    'executable': sys.executable,
}))
'@

    try {
        $probeOutput = & $Command @PrefixArgs -c $probeScript 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $probeOutput) {
            return $null
        }
        $probe = $probeOutput | ConvertFrom-Json
        $major = [int]$probe.version[0]
        $minor = [int]$probe.version[1]
        if ($major -ne 3 -or $minor -lt 10 -or $minor -gt 12) {
            return $null
        }
        if ([string]$probe.bits -ne "64bit") {
            return $null
        }
        return [PSCustomObject]@{
            Command = $Command
            PrefixArgs = $PrefixArgs
            Executable = [string]$probe.executable
            Version = "$major.$minor"
        }
    }
    catch {
        return $null
    }
}

try {
    $selectedPython = $null
    $pyLauncher = Get-Command "py" -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $pyLauncher) {
        foreach ($version in @("3.11", "3.12", "3.10")) {
            $candidate = Test-PythonCandidate -Command $pyLauncher.Path -PrefixArgs @("-$version")
            if ($null -ne $candidate) {
                $selectedPython = $candidate
                break
            }
        }
    }

    if ($null -eq $selectedPython) {
        $pythonCommand = Get-Command "python" -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -ne $pythonCommand) {
            $selectedPython = Test-PythonCandidate -Command $pythonCommand.Path
        }
    }

    if ($null -eq $selectedPython) {
        [Console]::Error.WriteLine(
            "No supported 64-bit Python found. Install Python 3.10, 3.11, or 3.12 and retry."
        )
        exit 2
    }

    $installer = Join-Path $PSScriptRoot "scripts\full_setup.py"
    & $selectedPython.Executable $installer @InstallerArgs
    $exitCode = $LASTEXITCODE
    if ($null -eq $exitCode) {
        exit 2
    }
    exit $exitCode
}
catch {
    [Console]::Error.WriteLine("Auto-Cut setup could not start a supported Python interpreter.")
    exit 2
}
