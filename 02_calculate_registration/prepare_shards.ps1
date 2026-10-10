param(
    [Parameter(Mandatory=$true)][string]$Root,
    [int]$Workers = 8,
    [string]$OutputDirectory = (Join-Path $PSScriptRoot 'manifests')
)
$ErrorActionPreference = 'Stop'
if ($Workers -lt 1) { throw 'Workers must be positive.' }
if (-not (Test-Path -LiteralPath $Root -PathType Container)) { throw "NAS root unavailable: $Root" }
if (Test-Path -LiteralPath $OutputDirectory) { throw "Manifest directory exists. Preserve the current assignment and choose a new directory: $OutputDirectory" }

$suffix = @('S210','S360','Leica','Olympus','P1000','Pramana','Roche','Zeiss')
$ihc = @('CD1A','CD20','CD68','CD163','CK8','Collagen IV','Ki-67','vimentin') | ForEach-Object { "Hum Liv PDAC $_" }
$eligible = [System.Collections.Generic.List[object]]::new()
$excluded = [System.Collections.Generic.List[object]]::new()
foreach ($slide in (Get-ChildItem -LiteralPath $Root -Directory | Sort-Object Name)) {
    $folder = Join-Path $slide.FullName '2x'
    $reason = $null
    if ($slide.Name -in $ihc) { $reason = 'IHC_excluded' }
    elseif (-not (Test-Path -LiteralPath $folder -PathType Container)) { $reason = 'missing_2x' }
    elseif ((Test-Path -LiteralPath (Join-Path $folder 'registered')) -or (Test-Path -LiteralPath (Join-Path $folder 'TA'))) { $reason = 'existing_or_partial_output' }
    else {
        $files = @(Get-ChildItem -LiteralPath $folder -File)
        $expected = @($suffix | ForEach-Object { "$($slide.Name)_$_.tif" })
        $actual = @($files | Where-Object { $_.Extension -eq '.tif' })
        $missing = @($expected | Where-Object { $_ -notin $actual.Name })
        $unexpected = @($files | Where-Object { $_.Name -notin $expected })
        if ($actual.Count -ne 8 -or $missing.Count -gt 0 -or $unexpected.Count -gt 0) { $reason = 'missing_or_unexpected_files' }
        else {
            $bytes = ($actual | Measure-Object Length -Sum).Sum
            $eligible.Add([pscustomobject]@{slide_id=$slide.Name; input_bytes=[long]$bytes})
        }
    }
    if ($reason) { $excluded.Add([pscustomobject]@{slide_id=$slide.Name; reason=$reason}) }
}

# Largest-first assignment balances a rough cost proxy while remaining fixed.
$loads = @(for ($i=0; $i -lt $Workers; $i++) { [long]0 })
$counts = @(for ($i=0; $i -lt $Workers; $i++) { 0 })
$assignment = [System.Collections.Generic.List[object]]::new()
foreach ($item in ($eligible | Sort-Object @{Expression='input_bytes';Descending=$true}, @{Expression='slide_id';Descending=$false})) {
    $worker = 0
    for ($i=1; $i -lt $Workers; $i++) { if ($loads[$i] -lt $loads[$worker]) { $worker=$i } }
    $assignment.Add([pscustomobject]@{worker_id=$worker+1; slide_id=$item.slide_id; input_bytes=$item.input_bytes})
    $loads[$worker] += $item.input_bytes
    $counts[$worker]++
}
if (@($assignment.slide_id | Select-Object -Unique).Count -ne $assignment.Count) { throw 'Duplicate slide in assignment.' }
New-Item -ItemType Directory -Path $OutputDirectory -ErrorAction Stop | Out-Null
$assignment | Sort-Object worker_id,slide_id | Export-Csv -LiteralPath (Join-Path $OutputDirectory 'assignment.csv') -NoTypeInformation -Encoding UTF8
$excluded | Export-Csv -LiteralPath (Join-Path $OutputDirectory 'excluded.csv') -NoTypeInformation -Encoding UTF8
for ($i=1; $i -le $Workers; $i++) {
    $assignment | Where-Object worker_id -eq $i | Sort-Object slide_id | Select-Object slide_id |
        Export-Csv -LiteralPath (Join-Path $OutputDirectory ("worker_{0:D2}.csv" -f $i)) -NoTypeInformation -Encoding UTF8
}
[pscustomobject]@{Eligible=$eligible.Count;Excluded=$excluded.Count;Workers=$Workers;ManifestDirectory=$OutputDirectory}
for ($i=0; $i -lt $Workers; $i++) { [pscustomobject]@{Worker=$i+1;Slides=$counts[$i];InputGiB=[math]::Round($loads[$i]/1GB,2)} }
