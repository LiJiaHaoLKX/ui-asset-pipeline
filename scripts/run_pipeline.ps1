param(
  [Parameter(Mandatory=$true)][string]$ReferenceImage,
  [Parameter(Mandatory=$true)][string]$RegionsJson,
  [int]$Padding = 32,
  [int]$Workers = 0
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Get-Command python -ErrorAction Stop
$crops = Join-Path $projectRoot 'regions/crops'
$raw = Join-Path $projectRoot 'assets/raw'
$split = Join-Path $projectRoot 'assets/split'
$runs = Join-Path $projectRoot 'runs/gpt-image'

& $python.Source (Join-Path $PSScriptRoot 'crop_regions.py') $ReferenceImage $RegionsJson $crops --padding $Padding
& $python.Source (Join-Path $PSScriptRoot 'extract_regions.py') (Join-Path $crops 'regions.normalized.json') $crops $raw $runs --workers $Workers
& $python.Source (Join-Path $PSScriptRoot 'split_extractions.py') (Join-Path $crops 'regions.normalized.json') $raw $split

Write-Host "Pipeline completed. Review: $split"
