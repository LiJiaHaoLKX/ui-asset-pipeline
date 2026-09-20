param(
  [string]$Url = 'http://127.0.0.1:8765/src/index.html',
  [Parameter(Mandatory=$true)][string]$Output,
  [int]$Width = 752,
  [int]$Height = 1344
)

$ErrorActionPreference = 'Stop'
$chromeCandidates = @(
  'C:\Program Files\Google\Chrome\Application\chrome.exe',
  'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
)
$browser = $chromeCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $browser) { throw 'Chrome or Edge was not found.' }

$outputPath = [System.IO.Path]::GetFullPath($Output)
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $outputPath) | Out-Null
$profile = Join-Path $env:TEMP ('ui-asset-pipeline-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $profile | Out-Null
try {
  $arguments = @(
    '--headless', '--disable-gpu', '--no-first-run', '--hide-scrollbars',
    '--run-all-compositor-stages-before-draw', '--virtual-time-budget=1800',
    '--force-device-scale-factor=1', "--window-size=$Width,$Height",
    "--user-data-dir=$profile", "--screenshot=$outputPath", $Url
  )
  $process = Start-Process -FilePath $browser -ArgumentList $arguments -WindowStyle Hidden -PassThru
  $process.WaitForExit(30000) | Out-Null
  $deadline = [DateTime]::UtcNow.AddSeconds(10)
  while (-not (Test-Path -LiteralPath $outputPath) -and [DateTime]::UtcNow -lt $deadline) {
    Start-Sleep -Milliseconds 100
  }
  if (-not (Test-Path -LiteralPath $outputPath)) { throw "Browser did not create $outputPath" }
  Write-Host $outputPath
} finally {
  $resolvedProfile = [System.IO.Path]::GetFullPath($profile)
  $resolvedTemp = [System.IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
  if ($resolvedProfile.StartsWith($resolvedTemp) -and (Split-Path -Leaf $resolvedProfile).StartsWith('ui-asset-pipeline-')) {
    Remove-Item -LiteralPath $resolvedProfile -Recurse -Force
  }
}
