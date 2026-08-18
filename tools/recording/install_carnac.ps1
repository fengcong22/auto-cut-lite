#Requires -Version 5.0
<#
.SYNOPSIS
  将 Carnac 便携包安装到本目录 carnac_bundle/（与 install_carnac.py 行为一致）。
#>
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
python.exe "$PSScriptRoot\install_carnac.py"
exit $LASTEXITCODE
