param(
    [Parameter(Mandatory=$true)][string]$SlideId,
    [switch]$Apply,
    [Parameter(Mandatory=$true)][string]$Root
)
$ErrorActionPreference = 'Stop'
if ($SlideId -match '[\\/]' -or $SlideId.Contains('..')) { throw 'Invalid slide ID.' }
$folder = Join-Path (Join-Path $Root $SlideId) '2x'
if (-not (Test-Path -LiteralPath $folder -PathType Container)) { throw "Slide missing: $folder" }
$suffix = @('S210','S360','Leica','Olympus','P1000','Pramana','Roche','Zeiss')
$expected = @($suffix | ForEach-Object { "${SlideId}_$_.tif" })
$inputs = @(Get-ChildItem -LiteralPath $folder -File -Filter '*.tif')
if ($inputs.Count -ne 8 -or @($expected | Where-Object { $_ -notin $inputs.Name }).Count -gt 0) { throw 'The eight expected TIFFs are not present.' }
$registered = Join-Path $folder 'registered'
$ta = Join-Path $folder 'TA'
if (-not (Test-Path -LiteralPath $registered) -and -not (Test-Path -LiteralPath $ta)) { throw 'No partial outputs found.' }
$warps = Join-Path $registered 'elastic registration\save_warps'
$globalCount = @(Get-ChildItem -LiteralPath $warps -File -Filter '*.mat' -ErrorAction SilentlyContinue).Count
$elasticCount = @(Get-ChildItem -LiteralPath (Join-Path $warps 'D') -File -Filter '*.mat' -ErrorAction SilentlyContinue).Count
if ($globalCount -ge 8 -and $elasticCount -ge 7) { throw 'Warp files appear complete; review manually before archiving.' }
Write-Output "Slide: $SlideId; global warps: $globalCount/8; elastic warps: $elasticCount/7"
if (-not $Apply) { Write-Output 'Preview only. Rerun with -Apply after all workers have stopped touching this slide.'; return }

$archive = Join-Path $folder ('registration_partial_archive\' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
$resolvedFolder = [System.IO.Path]::GetFullPath($folder).TrimEnd('\')
$resolvedArchive = [System.IO.Path]::GetFullPath($archive)
if (-not $resolvedArchive.StartsWith($resolvedFolder + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Archive path escaped the slide folder.' }
New-Item -ItemType Directory -Path $archive -ErrorAction Stop | Out-Null
foreach ($name in @('registered','TA')) {
    $source = Join-Path $folder $name
    if (Test-Path -LiteralPath $source) { Move-Item -LiteralPath $source -Destination $archive -ErrorAction Stop }
}
Write-Output "Partial outputs moved to: $archive"
