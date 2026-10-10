param(
    [Parameter(Mandatory=$true)][ValidateRange(1,8)][int]$WorkerId,
    [Parameter(Mandatory=$true)][string]$Root,
    [string]$ManifestDirectory = (Join-Path $PSScriptRoot 'manifests'),
    [string]$Matlab = 'C:\Program Files\MATLAB\R2024b\bin\matlab.exe'
)
$ErrorActionPreference = 'Stop'
$package = $PSScriptRoot
$manifest = Join-Path $ManifestDirectory ('worker_{0:D2}.csv' -f $WorkerId)
if (-not (Test-Path -LiteralPath $Matlab -PathType Leaf)) { throw "MATLAB missing: $Matlab" }
if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { throw "Manifest missing: $manifest" }
if (@(Get-Process MATLAB -ErrorAction SilentlyContinue).Count -gt 0) { throw 'MATLAB is already running on this computer.' }
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$stdout = Join-Path $package ('worker_{0:D2}_{1}.log' -f $WorkerId,$stamp)
$stderr = Join-Path $package ('worker_{0:D2}_{1}.err.log' -f $WorkerId,$stamp)
$matlabPath = $package.Replace("'", "''")
$matlabManifest = $manifest.Replace("'", "''")
$matlabRoot = $Root.Replace("'", "''")
$command = "addpath('$matlabPath'); run_worker($WorkerId,'$matlabManifest','$matlabRoot')"
$process = Start-Process -FilePath $Matlab -ArgumentList "-batch `"$command`"" `
    -WorkingDirectory $package -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
Write-Output "Worker $WorkerId MATLAB PID: $($process.Id)"
Write-Output "Progress log: $stdout"
Write-Output "Error log: $stderr"
