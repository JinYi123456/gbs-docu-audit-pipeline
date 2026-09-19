# ============================================================================
# SDOC Hackathon 2026 · bootstrap（PowerShell 版，等价于 scripts/bootstrap.sh）
# ============================================================================
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
$root = (Get-Location).Path
Write-Host "[bootstrap] root = $root"

New-Item -ItemType Directory -Force -Path 'official-zips' | Out-Null
foreach ($zip in @('sdoc-hackathon-bundle.zip','sdoc-hackathon-docker.zip')) {
  if (Test-Path $zip) {
    Move-Item -Force $zip (Join-Path 'official-zips' $zip)
    Write-Host "[bootstrap] moved $zip -> official-zips/"
  }
}
$bundleZip = 'official-zips/sdoc-hackathon-bundle.zip'
$dockerZip = 'official-zips/sdoc-hackathon-docker.zip'

Add-Type -AssemblyName System.IO.Compression.FileSystem
function Expand-Into($zipPath, $prefix, $dest) {
  $zip = [System.IO.Compression.ZipFile]::OpenRead((Resolve-Path $zipPath))
  try {
    foreach ($entry in $zip.Entries) {
      if ($prefix -and -not $entry.FullName.StartsWith($prefix)) { continue }
      $relative = $entry.FullName
      $target = Join-Path $dest $relative
      if ($entry.Length -eq 0 -and $entry.FullName.EndsWith('/')) { continue }
      New-Item -ItemType Directory -Force -Path (Split-Path $target -Parent) | Out-Null
      [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
    }
  } finally { $zip.Dispose() }
}

# 1. 参赛数据包 -> data/
New-Item -ItemType Directory -Force -Path 'data' | Out-Null
if (-not (Test-Path 'data/loader.py') -and (Test-Path $bundleZip)) {
  Write-Host '[bootstrap] 解压参赛数据包 -> data/'
  Expand-Into $bundleZip '' 'data'
}
New-Item -ItemType File -Force -Path 'data/.gitkeep' | Out-Null

# 2. 官方评分服务 -> server/ + docker-compose.yml
New-Item -ItemType Directory -Force -Path 'server' | Out-Null
if (-not (Test-Path 'server/app.py') -and (Test-Path $dockerZip)) {
  Write-Host '[bootstrap] 解压官方评分服务 -> server/'
  Expand-Into $dockerZip 'server/' '.'
  Expand-Into $dockerZip 'docker-compose.yml' '.'
}
New-Item -ItemType File -Force -Path 'server/.gitkeep' | Out-Null

# 3. 答案键 -> eval/private/
New-Item -ItemType Directory -Force -Path 'eval/private','eval/generator','eval/report' | Out-Null
if (-not (Test-Path 'eval/private/ground_truth.json') -and (Test-Path $dockerZip)) {
  Write-Host '[bootstrap] 抽取答案键 -> eval/private/ground_truth.json（仅本地开发回归用）'
  Expand-Into $dockerZip 'data_v2/ground_truth.json' 'eval/private'
  if (Test-Path 'eval/private/data_v2/ground_truth.json') {
    Move-Item -Force 'eval/private/data_v2/ground_truth.json' 'eval/private/ground_truth.json'
    Remove-Item -Recurse -Force 'eval/private/data_v2'
  }
}

# 4. 生成器
if (-not (Test-Path 'eval/generator/generate.py') -and (Test-Path $dockerZip)) {
  Write-Host '[bootstrap] 抽取生成器 -> eval/generator/'
  Expand-Into $dockerZip 'data_v2/' 'eval/generator'
  foreach ($f in @('generate.py','pools.py','render.py','shipment.py','emails.py','edgecases.py')) {
    $from = "eval/generator/data_v2/$f"
    if (Test-Path $from) { Move-Item -Force $from "eval/generator/$f" }
  }
  if (Test-Path 'eval/generator/data_v2') { Remove-Item -Recurse -Force 'eval/generator/data_v2' }
}

# 5. 修正 docker-compose.yml 挂载路径
if (Test-Path 'docker-compose.yml') {
  $compose = Get-Content 'docker-compose.yml' -Raw
  if ($compose -match './data_v2') {
    Write-Host '[bootstrap] 修正 docker-compose.yml 的 volume 路径'
    $compose = $compose -replace '\./data_v2/ground_truth\.json:/secrets/ground_truth\.json:ro', './eval/private/ground_truth.json:/secrets/ground_truth.json:ro'
    $compose = $compose -replace '\./data_v2:/data:ro', './data:/data:ro'
    Set-Content 'docker-compose.yml' $compose -NoNewline
  }
  Select-String -Path 'docker-compose.yml' -Pattern '^\s+- \./' | ForEach-Object { Write-Host "  $_" }
}

Write-Host ''
Write-Host '[bootstrap] 完成。下一步：'
Write-Host '  python scripts/verify_dataset.py'
Write-Host '  make run20'
