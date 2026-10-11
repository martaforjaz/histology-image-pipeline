param(
    [Parameter(Mandatory=$true)][ValidateRange(1,8)][int]$WorkerId,
    [Parameter(Mandatory=$true)][string]$Root,
    [string]$ManifestDirectory = (Join-Path $PSScriptRoot 'manifests_partial'),
    [ValidateRange(10,600)][int]$PollSeconds = 60,
    [ValidateRange(1,72)][int]$MaxWaitHours = 24
)
$ErrorActionPreference = 'Stop'
$deadline = (Get-Date).AddHours($MaxWaitHours)
Write-Output "Waiting for local MATLAB processes to finish before partial worker $WorkerId."
while (@(Get-Process MATLAB -ErrorAction SilentlyContinue).Count -gt 0) {
    if ((Get-Date) -ge $deadline) { throw 'Wait deadline reached without a free MATLAB slot.' }
    Start-Sleep -Seconds $PollSeconds
}
Write-Output "MATLAB slot free at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'); launching partial worker $WorkerId."
& (Join-Path $PSScriptRoot 'launch_partial_worker.ps1') -WorkerId $WorkerId -Root $Root -ManifestDirectory $ManifestDirectory
