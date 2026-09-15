param(
    [ValidateSet("Chrome", "Edge")]
    [string]$Browser = "Chrome",
    [int]$Port = 9222
)

$ErrorActionPreference = "Stop"
$profile = Join-Path $env:LOCALAPPDATA "VideoGet\DouyinBrowser"

$chromeCandidates = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$edgeCandidates = @(
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
)

$candidates = if ($Browser -eq "Edge") { $edgeCandidates } else { $chromeCandidates }
$exe = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $exe) {
    throw "$Browser executable was not found. Install Chrome/Edge or edit scripts/start_douyin_browser.ps1 with its path."
}

New-Item -ItemType Directory -Force -Path $profile | Out-Null

Write-Host "Starting dedicated $Browser profile for VideoGet Douyin..." -ForegroundColor Cyan
Write-Host "Profile: $profile"
Write-Host "CDP: http://localhost:$Port"
Write-Host ""
Write-Host "Security: this debug port controls this dedicated browser. Keep it on your private machine/network and do not expose port $Port to the public Internet." -ForegroundColor Yellow

$args = @(
    "--remote-debugging-port=$Port",
    "--remote-debugging-address=0.0.0.0",
    "--user-data-dir=$profile",
    "--no-first-run",
    "--no-default-browser-check",
    "https://www.douyin.com/"
)

Start-Process -FilePath $exe -ArgumentList $args

Start-Sleep -Seconds 2
try {
    $version = Invoke-RestMethod -Uri "http://localhost:$Port/json/version" -TimeoutSec 3
    Write-Host "CDP ready: $($version.Browser)" -ForegroundColor Green
    Write-Host "Log in to Douyin in this window. If Douyin shows verification, complete it manually."
    Write-Host ""
    Write-Host "Docker test:"
    Write-Host "  docker compose exec videoget curl http://host.docker.internal:$Port/json/version"
}
catch {
    Write-Warning "Browser started but CDP did not answer yet. Wait a few seconds and run:"
    Write-Host "  curl.exe http://localhost:$Port/json/version"
}
